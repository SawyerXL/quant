"""
盘中异动监测 — 个人持仓 + 跟踪股票的实时异动扫描
逐只检查四项:
  ① 主力资金反转 (近数日趋势 vs 今日, 昨出今进 / 昨进今出)
  ② 止损触发 (现价 vs 成本, -12%硬止损 / -8%预警)
  ③ 涨停/跌停/开板 (按板块涨跌幅上限判定, 触板后回落=开板/炸板)
  ④ 关键MA突破 (距MA10<1% 或 今日上穿/下穿MA10)

数据源: Sina实时(价/量, 稳) + 本地日线(MA) + MCP(主力资金, 东财封IP只能走MCP)
触发任一条件即发邮件告警。

用法: python scripts/intraday_anomaly_scan.py [--send]
注意: 监测的是【个人账户】(config/my_holdings.csv), 与QMT账户严格分离。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import pandas as pd
from datetime import date, datetime
from loguru import logger

from data.storage import load_daily

logger.add("logs/intraday_anomaly.log", rotation="7 days", retention="14 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")
# 跟踪但未持有的关注股 (散见于 monitor_watchlist / realtime_flow_monitor)
WATCH_ONLY = {"300124": "汇川技术", "300408": "三环集团", "600111": "北方稀土"}
# 三板/权证等无常规涨跌停与主力资金口径, 跳过异动判定
SKIP_CODES = {"400286"}

# 止损阈值 (个人账户纪律)
STOP_HARD = -0.12
STOP_WARN = -0.08
# 主力资金反转阈值 (万元); 盘初资金量小, 阈值取偏低
FLOW_TODAY_MIN = 800     # 今日主力净额绝对值下限
FLOW_PRIOR_MIN = 2000    # 前几日主力净额绝对值下限


def _limit_pct(code: str, name: str) -> float:
    """按板块给出当日涨跌幅上限(单边)。"""
    if "ST" in name.upper():
        return 0.05
    if code.startswith(("30", "68")):   # 创业板 / 科创板
        return 0.20
    if code.startswith(("4", "8")):     # 北交所
        return 0.30
    return 0.10                          # 沪深主板


def fetch_sina(code: str) -> dict | None:
    """Sina实时行情: 现价/今开/昨收/最高/最低。东财封本服务器IP, Sina是主源。"""
    exch = "sh" if code.startswith("6") else ("bj" if code.startswith(("4", "8")) else "sz")
    try:
        r = requests.get(f"http://hq.sinajs.cn/list={exch}{code}",
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=4)
        d = r.text.split('"')[1].split(",")
        if len(d) < 6:
            return None
        price = float(d[3]) or float(d[2])   # 停牌/集合竞价前现价为0, 落昨收
        return {
            "name": d[0], "open": float(d[1]), "prev_close": float(d[2]),
            "price": price, "high": float(d[4]), "low": float(d[5]),
        }
    except Exception as e:
        logger.warning(f"Sina {code} 失败: {e}")
        return None


def fetch_flow(mcp, code: str) -> dict | None:
    """MCP主力资金: 今日净额 + 连续流入天数 + 五日净额 (万元)。"""
    try:
        df = mcp.get_capital_flow(code, date.today().strftime("%Y-%m-%d"))
        if df is None or df.empty:
            return None
        r = df.iloc[0]
        return {
            "today": float(r.get("主力净额(万元)", 0) or 0),
            "streak": float(r.get("主力资金连续流入天数(天)", 0) or 0),
            "net5": float(r.get("五日主力资金净额(万元)", 0) or 0),
        }
    except Exception as e:
        logger.warning(f"MCP flow {code} 失败: {e}")
        return None


def ma10_of(code: str) -> float:
    """本地日线最近10个收盘的MA10 (截至昨收, 用于与实时价对比)。"""
    df = load_daily(code, (date.today() - pd.Timedelta(days=40)).strftime("%Y-%m-%d"),
                    date.today().strftime("%Y-%m-%d"))
    if df.empty or "close" not in df.columns:
        return 0.0
    closes = df.sort_values("date")["close"].dropna()
    # 本地最新一根通常是昨收(今日盘中未落库), 取最近10根
    if len(closes) < 10:
        return 0.0
    return float(closes.tail(10).mean())


def scan_one(code: str, name_hint: str, cost: float, shares: int, mcp) -> dict:
    q = fetch_sina(code)
    if q is None:
        return {"code": code, "name": name_hint, "ok": False}
    name = q["name"] or name_hint
    price, prev, high, low = q["price"], q["prev_close"], q["high"], q["low"]
    chg = (price / prev - 1) if prev else 0.0
    ma10 = ma10_of(code)
    flow = fetch_flow(mcp, code)

    triggers = []   # 触发告警的条目
    notes = []      # 非告警的观察

    # ① 主力资金反转
    flow_str = "—"
    if flow:
        today, net5, streak = flow["today"], flow["net5"], flow["streak"]
        prior = net5 - today   # 约前4日净额, 近似"昨"
        flow_str = f"今{today/1e4:+.2f}亿 连{streak:+.0f}d"
        meaningful = abs(today) > FLOW_TODAY_MIN and abs(prior) > FLOW_PRIOR_MIN
        if meaningful and prior < 0 and today > 0:
            triggers.append(f"🔄 主力反转-昨出今进 (前{prior/1e4:+.2f}亿→今{today/1e4:+.2f}亿)")
        elif meaningful and prior > 0 and today < 0:
            triggers.append(f"🔴 主力反转-昨进今出 (前{prior/1e4:+.2f}亿→今{today/1e4:+.2f}亿)")

    # ② 止损触发 (仅持仓)
    pnl = (price / cost - 1) if cost else None
    if pnl is not None and shares > 0:
        if pnl <= STOP_HARD:
            triggers.append(f"🔴 硬止损 {pnl:+.1%} (成本{cost:.2f})")
        elif pnl <= STOP_WARN:
            triggers.append(f"🟡 接近止损 {pnl:+.1%} (成本{cost:.2f})")

    # ③ 涨停/跌停/开板
    lim = _limit_pct(code, name)
    up = round(prev * (1 + lim), 2)
    dn = round(prev * (1 - lim), 2)
    if price >= up - 0.005:
        triggers.append(f"🚀 涨停 {price:.2f}")
    elif high >= up - 0.005 and price < up - 0.005:
        triggers.append(f"⚠️ 涨停开板/炸板 (触{up:.2f} 现{price:.2f})")
    if price <= dn + 0.005:
        triggers.append(f"💥 跌停 {price:.2f}")
    elif low <= dn + 0.005 and price > dn + 0.005:
        triggers.append(f"⚠️ 跌停开板 (触{dn:.2f} 现{price:.2f})")

    # ④ 关键MA突破 (距MA10<1% 或 上穿/下穿)
    ma_str = "—"
    if ma10 > 0:
        dist = price / ma10 - 1
        ma_str = f"{ma10:.2f}({dist:+.1%})"
        prev_side = prev - ma10   # 昨收相对MA10
        if prev_side < 0 <= (price - ma10):
            triggers.append(f"📈 上穿MA10 ({ma10:.2f})")
        elif prev_side > 0 >= (price - ma10):
            triggers.append(f"📉 下穿MA10 ({ma10:.2f})")
        elif abs(dist) < 0.01:
            notes.append(f"贴近MA10 {dist:+.1%}")

    return {
        "code": code, "name": name, "ok": True, "held": shares > 0,
        "price": price, "chg": chg, "pnl": pnl, "ma10": ma10,
        "flow_str": flow_str, "ma_str": ma_str,
        "triggers": triggers, "notes": notes,
    }


def build_universe():
    """持仓(my_holdings) + 跟踪股; 返回 [(code, name, cost, shares)]。"""
    uni = []
    df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    held_codes = set()
    for _, r in df.iterrows():
        if not r.get("monitor", True):
            continue
        code = r["code"]
        if code in SKIP_CODES:
            continue
        cost = float(r["cost_price"]) if pd.notna(r.get("cost_price")) else 0.0
        shares = int(r["shares"]) if pd.notna(r.get("shares")) else 0
        uni.append((code, str(r["name"]), cost, shares))
        held_codes.add(code)
    for code, name in WATCH_ONLY.items():
        if code not in held_codes:
            uni.append((code, name, 0.0, 0))
    return uni


def run(send_email=False):
    now = datetime.now()
    uni = build_universe()
    logger.info(f"盘中异动扫描 {now:%H:%M} 共{len(uni)}只")

    from data.source.mcp_source import MCPSource
    mcp = MCPSource()

    results = [scan_one(c, n, cost, sh, mcp) for c, n, cost, sh in uni]
    ok = [r for r in results if r["ok"]]
    triggered = [r for r in ok if r["triggers"]]

    # ---- 逐只输出 ----
    lines = []
    lines.append(f"盘中异动监测 {now:%m/%d %H:%M}  个人账户 {len(ok)}/{len(uni)}只")
    lines.append(f"触发异动: {len(triggered)}只")
    lines.append("")
    lines.append(f"  {'代码':<7}{'名称':<7}{'现价':>8}{'涨跌':>7}{'盈亏':>7}  {'MA10(距)':>13}  {'主力资金':>16}")
    lines.append("  " + "─" * 78)
    for r in sorted(ok, key=lambda x: (not x["triggers"], not x["held"])):
        pnl_s = f"{r['pnl']:+.1%}" if r["pnl"] is not None and r["held"] else "  —"
        flag = "★" if r["triggers"] else " "
        lines.append(f"{flag} {r['code']:<7}{r['name']:<7}{r['price']:>8.2f}{r['chg']:>+7.1%}{pnl_s:>7}"
                     f"  {r['ma_str']:>13}  {r['flow_str']:>16}")
        for t in r["triggers"]:
            lines.append(f"      → {t}")
        for nt in r["notes"]:
            lines.append(f"      · {nt}")

    lines.append("")
    lines.append("━" * 60)
    if triggered:
        lines.append(f"⚠️ 触发异动清单 ({len(triggered)}只):")
        for r in triggered:
            tag = "持仓" if r["held"] else "跟踪"
            lines.append(f"  [{tag}] {r['code']} {r['name']} ¥{r['price']:.2f} {r['chg']:+.1%}")
            for t in r["triggers"]:
                lines.append(f"        {t}")
    else:
        lines.append("✅ 无异动触发 — 所有持仓/跟踪股在正常区间")
    lines.append("")
    lines.append("说明: ①主力资金反转 ②止损 ③涨停跌停开板 ④MA10突破。资金流仅MCP口径, 盘初量小仅供参考。")

    report = "\n".join(lines)
    print(report)

    # ---- 触发才发邮件 ----
    if triggered and send_email:
        from monitoring.alerts import _send_email
        subject = f"[量化异动] {now:%m/%d %H:%M} {len(triggered)}只触发"
        _send_email(subject, report)
        logger.info(f"异动告警邮件已发送: {len(triggered)}只")
    elif not triggered and send_email:
        logger.info("无异动, 不发邮件")

    return triggered


if __name__ == "__main__":
    run(send_email="--send" in sys.argv)
