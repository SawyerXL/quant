"""
开盘资金流快报 — MCP实时主力资金, 对比昨日, 标记反转
个人账户(config/my_holdings.csv), 与QMT账户分离。

模式:
  --mode holdings  持仓快报(默认, 09:31): 仅持仓, 现价/昨主力/今主力/方向/建议, 求快
  --mode full      开盘资金流(09:35): 持仓+跟踪, 超大单/大单/小单细分, 反转标记, 北方稀土建仓检查
  --snapshot       盘后存档(15:05): 把今日主力资金写入 flow_eod_YYYYMMDD.json, 供次日作"昨主力"
  --send           触发/正常均发邮件

"昨主力"来源: 优先读最近一个交易日的 flow_eod 存档; 若无(首次运行), 用 MCP 的
(五日净额-今日)/4 作前几日日均代理, 并以 * 标注。存档机制建立后次日起为精确值。

数据: MCP get_capital_flow(主力资金) + Sina实时价 + 本地日线MA10
"""
import sys, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import pandas as pd
from datetime import date, datetime
from loguru import logger

from data.storage import load_daily

logger.add("logs/flow_flash.log", rotation="7 days", retention="14 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")
CACHE_DIR = Path("logs/cache")
WATCH_ONLY = {"300124": "汇川技术", "300408": "三环集团", "600111": "北方稀土"}
SKIP_CODES = {"400286"}          # 三板/权证, 无常规主力资金口径
BUILD_TARGET = "600111"          # 北方稀土: 抢筹+站上MA10 则提示可建仓

# 方向阈值 (亿元)
T_STRONG = 0.5     # 抢筹/踩踏: 主力净额
T_BIG = 0.2        # 抢筹/踩踏: 超大单净额
T_FLOW = 0.05      # 流入/流出
# 反转阈值 (亿元)
R_PRIOR = 0.2      # 昨(前几日)绝对值下限
R_TODAY = 0.08     # 今日绝对值下限


def rt_quote(code: str) -> dict:
    """Sina实时: 现价+昨收 (东财封本机IP, Sina为主源)。"""
    exch = "sh" if code.startswith("6") else ("bj" if code.startswith(("4", "8")) else "sz")
    try:
        r = requests.get(f"http://hq.sinajs.cn/list={exch}{code}",
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=4)
        d = r.text.split('"')[1].split(",")
        price = float(d[3]) or float(d[2])
        prev = float(d[2])
        return {"price": price, "prev": prev, "chg": (price / prev - 1) if prev else 0.0}
    except Exception:
        return {"price": 0.0, "prev": 0.0, "chg": 0.0}


def ma10_of(code: str) -> float:
    df = load_daily(code, (date.today() - pd.Timedelta(days=40)).strftime("%Y-%m-%d"),
                    date.today().strftime("%Y-%m-%d"))
    if df.empty or "close" not in df.columns:
        return 0.0
    closes = df.sort_values("date")["close"].dropna()
    return float(closes.tail(10).mean()) if len(closes) >= 10 else 0.0


def fetch_flow(mcp, code: str) -> dict | None:
    """MCP主力资金(今日实时, 亿元)。"""
    try:
        df = mcp.get_capital_flow(code, date.today().strftime("%Y-%m-%d"))
        if df is None or df.empty:
            return None
        r = df.iloc[0]
        g = lambda k: float(r.get(k, 0) or 0) / 1e4   # 万元→亿元
        return {
            "main": g("主力净额(万元)"),
            "big": g("超大单资金净额(万元)"),      # 超大单
            "large": g("大单资金净额(万元)"),       # 大单
            "mid": g("中单资金净额(万元)"),
            "small": g("小单资金净额(万元)"),       # 小单
            "streak": float(r.get("主力资金连续流入天数(天)", 0) or 0),
            "net5": g("五日主力资金净额(万元)"),
        }
    except Exception as e:
        logger.warning(f"MCP flow {code} 失败: {e}")
        return None


def load_prev_eod(today: str) -> tuple[dict, str]:
    """读取今天之前最近一个 flow_eod 存档作为'昨主力'。返回(dict, 日期str或'')。"""
    files = sorted(CACHE_DIR.glob("flow_eod_*.json"))
    for f in reversed(files):
        d = f.stem.replace("flow_eod_", "")
        if d < today.replace("-", ""):
            try:
                return json.loads(f.read_text(encoding="utf-8")), d
            except Exception:
                continue
    return {}, ""


def classify(flow: dict) -> str:
    """抢筹/流入/均衡/流出/踩踏。"""
    m, b = flow["main"], flow["big"]
    if m > T_STRONG and b > T_BIG:
        return "抢筹"
    if m < -T_STRONG and b < -T_BIG:
        return "踩踏"
    if m > T_FLOW:
        return "流入"
    if m < -T_FLOW:
        return "流出"
    return "均衡"


def reversal_of(prev_main, today_main) -> str:
    """昨 vs 今 反转判定。"""
    if prev_main is None:
        return ""
    if abs(prev_main) < R_PRIOR or abs(today_main) < R_TODAY:
        return ""
    if prev_main < 0 and today_main > 0:
        return "昨出今进"
    if prev_main > 0 and today_main < 0:
        return "昨买今卖"
    return ""


def advise(direction, rev, price, ma10) -> str:
    above = ma10 > 0 and price >= ma10
    if rev == "昨买今卖" or direction == "踩踏":
        return "减仓"
    if direction == "抢筹":
        return "加仓" if above else "持有"
    if direction == "流出":
        return "持有(盯)" if above else "减仓"
    if rev == "昨出今进":
        return "持有(资金回补)"
    return "持有"


def build_universe(mode: str):
    df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    uni, held = [], set()
    for _, r in df.iterrows():
        if not r.get("monitor", True) or r["code"] in SKIP_CODES:
            continue
        uni.append((r["code"], str(r["name"]), True))
        held.add(r["code"])
    if mode == "full":
        for code, name in WATCH_ONLY.items():
            if code not in held:
                uni.append((code, name, False))
    return uni


def snapshot(mcp, today: str):
    """盘后: 写入今日主力资金存档, 供次日作'昨主力'。"""
    uni = build_universe("full")
    out = {}
    for code, _, _ in uni:
        f = fetch_flow(mcp, code)
        if f:
            out[code] = {"main": f["main"], "big": f["big"], "large": f["large"], "small": f["small"]}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"flow_eod_{today.replace('-', '')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    logger.info(f"EOD资金存档: {len(out)}只 → {path.name}")
    print(f"✅ EOD资金存档 {len(out)}只 → {path}")


def yi(v):
    return f"{v:+.2f}亿"


def run(mode: str, send: bool):
    now = datetime.now()
    today = date.today().strftime("%Y-%m-%d")
    from data.source.mcp_source import MCPSource
    mcp = MCPSource()

    if mode == "snapshot":
        snapshot(mcp, today)
        return

    uni = build_universe(mode)
    prev_eod, prev_date = load_prev_eod(today)
    prev_tag = f"昨({prev_date[4:6]}/{prev_date[6:8]})" if prev_date else "昨*(前4日均)"

    rows = []
    for code, name, held in uni:
        f = fetch_flow(mcp, code)
        q = rt_quote(code)
        ma10 = ma10_of(code)
        if f is None:
            rows.append({"code": code, "name": name, "held": held, "ok": False})
            continue
        # 昨主力: EOD存档优先, 否则前4日日均代理
        if code in prev_eod:
            prev_main = prev_eod[code].get("main")
            prev_exact = True
        else:
            prev_main = (f["net5"] - f["main"]) / 4 if f["net5"] else None
            prev_exact = False
        direction = classify(f)
        rev = reversal_of(prev_main, f["main"])
        rows.append({
            "code": code, "name": name, "held": held, "ok": True,
            "price": q["price"], "chg": q["chg"], "ma10": ma10,
            "prev_main": prev_main, "prev_exact": prev_exact, "flow": f,
            "direction": direction, "rev": rev,
            "advise": advise(direction, rev, q["price"], ma10),
        })

    ok = [r for r in rows if r["ok"]]
    revs = [r for r in ok if r["rev"]]

    lines = []
    if mode == "holdings":
        lines.append(f"持仓资金快报 {now:%m/%d %H:%M}  个人账户 {len(ok)}只")
        lines.append(f"反转: {sum(1 for r in revs)}只 | {prev_tag}主力 vs 今日主力")
        lines.append("")
        lines.append(f"  {'代码':<7}{'名称':<7}{'现价':>8}{prev_tag+'主力':>12}{'今主力':>10}  {'方向':<5}{'建议':<9}")
        lines.append("  " + "─" * 74)
        for r in sorted(ok, key=lambda x: (x["advise"] == "持有", x["code"])):
            pm = (yi(r["prev_main"]) + ("" if r["prev_exact"] else "*")) if r["prev_main"] is not None else "—"
            rev_s = f" ⚡{r['rev']}" if r["rev"] else ""
            lines.append(f"  {r['code']:<7}{r['name']:<7}{r['price']:>8.2f}{pm:>12}{yi(r['flow']['main']):>10}"
                         f"  {r['direction']:<5}{r['advise']:<9}{rev_s}")
    else:  # full
        n_held = sum(1 for r in ok if r["held"])
        lines.append(f"开盘资金流(反转扫描) {now:%m/%d %H:%M}  持仓{n_held}+跟踪{len(ok)-n_held}={len(ok)}只")
        lines.append(f"反转标记: 昨出今进 {sum(1 for r in revs if r['rev']=='昨出今进')}只 / "
                     f"昨买今卖 {sum(1 for r in revs if r['rev']=='昨买今卖')}只 | {prev_tag}主力")
        lines.append("")
        lines.append(f"  {'代码':<7}{'名称':<7}{prev_tag:>9}{'今主力':>9}{'超大单':>9}{'大单':>9}{'小单':>9}  {'判断'}")
        lines.append("  " + "─" * 88)
        for r in sorted(ok, key=lambda x: (not x["rev"], not x["held"])):
            fl = r["flow"]
            pm = (yi(r["prev_main"]) + ("" if r["prev_exact"] else "*")) if r["prev_main"] is not None else "—"
            judge = r["direction"] + (f" ⚡{r['rev']}" if r["rev"] else "")
            tag = "" if r["held"] else "[跟踪]"
            lines.append(f"  {r['code']:<7}{r['name']:<7}{pm:>9}{yi(fl['main']):>9}{yi(fl['big']):>9}"
                         f"{yi(fl['large']):>9}{yi(fl['small']):>9}  {judge}{tag}")

        # 反转清单
        lines.append("")
        lines.append("━" * 56)
        if revs:
            lines.append(f"⚡ 资金反转 ({len(revs)}只):")
            for r in revs:
                arrow = "🔄可为多" if r["rev"] == "昨出今进" else "🔴主力撤"
                lines.append(f"  {arrow} {r['code']} {r['name']}: "
                             f"{prev_tag}{yi(r['prev_main'])}→今{yi(r['flow']['main'])} ({r['rev']})")
        else:
            lines.append("无显著资金反转")

        # 北方稀土建仓检查
        bt = next((r for r in ok if r["code"] == BUILD_TARGET), None)
        lines.append("")
        lines.append("━" * 56)
        if bt:
            fl = bt["flow"]
            grabbing = fl["main"] > T_FLOW and fl["big"] > 0   # 主力净流入且超大单为正=抢筹
            above = bt["ma10"] > 0 and bt["price"] >= bt["ma10"]
            can_build = grabbing and above
            lines.append(f"[北方稀土建仓检查] {BUILD_TARGET} 现价{bt['price']:.2f} MA10 {bt['ma10']:.2f}")
            lines.append(f"  抢筹: {'✅' if grabbing else '❌'}(今主力{yi(fl['main'])} 超大单{yi(fl['big'])}) | "
                         f"MA10上方: {'✅' if above else '❌'}({bt['price']/bt['ma10']-1:+.1%})" if bt["ma10"] else "  MA10缺失")
            lines.append(f"  → {'🟢 满足: 可考虑建仓' if can_build else '⚪ 不满足, 暂不建仓'}")
        else:
            lines.append("[北方稀土建仓检查] 未取到数据")

    lines.append("")
    lines.append("说明: 资金流MCP口径, 盘初量小仅参考。抢筹=主力>+0.5亿且超大单>+0.2亿; 踩踏=主力<-0.5亿且超大单<-0.2亿。")
    if not prev_date:
        lines.append("注: 昨主力用前4日日均代理(*), EOD存档从今日15:05起建立, 次日起为精确单日值。")

    report = "\n".join(lines)
    print(report)

    if send:
        from monitoring.alerts import _send_email
        title = "持仓资金快报" if mode == "holdings" else "开盘资金流反转"
        subj = f"[量化资金] {title} {now:%m/%d %H:%M}"
        if mode == "full" and revs:
            subj += f" {len(revs)}只反转"
        _send_email(subj, report)
        logger.info(f"{title}邮件已发送")

    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["holdings", "full"], default="holdings")
    ap.add_argument("--snapshot", action="store_true", help="盘后写EOD资金存档")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    run("snapshot" if a.snapshot else a.mode, a.send)
