"""
9:03 盘前自动检查：数据完整性 + MA10触发清单 + 外盘跳空提示。

按纪律输出四块：
  1. 交易日判断 + 数据新鲜度（持仓票最新bar vs 最近交易日，脏跳扫描）
  2. MA10-4d 触发清单（含三条豁免：RSI<30 / 浮盈>50%改MA20 / V反盘中判）
  3. 外盘跳空提示（只提示不操作 —— 五个信号回测后的定案）
  4. 汇总发邮件（monitoring.alerts.send_alert）

cron: 3 9 * * 1-5
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import csv
import pandas as pd
from datetime import date, datetime
from loguru import logger

logger.add("logs/preopen_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days")

MA10_DAYS = 4          # 连破MA10天数触发线
CB_CODES = {"110", "111", "113", "118", "123", "127", "128"}   # 转债前缀
ETF_CODES = {"5"}      # 5开头场内基金(ETF/LOF)


def _rsi(cl: pd.Series, n: int = 14) -> float:
    d = cl.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    return float(100 - 100 / (1 + ru.iloc[-1] / rd.iloc[-1])) if rd.iloc[-1] > 0 else 100.0


def main():
    from data.storage import load_daily, load_meta

    # ── 1. 交易日判断 ──
    cal = sorted(load_meta("trade_calendar")["trade_date"].astype(str).tolist())
    today = str(date.today())
    if today not in cal:
        logger.info(f"{today} 非交易日，跳过盘前检查")
        return
    last_td = max(d for d in cal if d < today)

    problems, lines = [], []
    lines.append(f"盘前检查 {today}（数据截至 {last_td}）")

    # ── 2. 数据完整性：持仓票 + 上证 ──
    stale, checked = [], 0
    try:
        rows = list(csv.DictReader(open("config/my_holdings.csv")))
    except Exception:
        rows = []
    for r in rows:
        c = str(r.get("code", "")).strip().zfill(6)
        if not c.isdigit():
            continue
        try:
            if c[:3] in CB_CODES:
                d = pd.read_parquet(f"data_store/convertible_bonds/daily/{c}.parquet")
                col = "date"
            else:
                d = load_daily(c, "2026-01-01", today)
                col = "date"
            checked += 1
            if d.empty:
                stale.append(f"{c} {r['name']}: 无数据文件")
                continue
            latest = str(pd.to_datetime(d[col]).max())[:10]
            if latest < last_td:
                stale.append(f"{c} {r['name']}: 最新{latest} < {last_td}")
        except Exception as e:
            stale.append(f"{c}: 读取失败 {str(e)[:40]}")
    sh = load_daily("000001", "2026-01-01", today)
    if sh.empty or str(pd.to_datetime(sh["date"]).max())[:10] < last_td:
        stale.append("上证指数: 未更新")
    if stale:
        problems.append(f"数据缺失 {len(stale)}项: {'; '.join(stale[:6])}")
        lines.append(f"⚠️ 数据缺失 {len(stale)}项（前6: {'; '.join(stale[:6])}）")
    else:
        lines.append(f"✅ 数据完整性: {checked}只持仓+上证 全部到 {last_td}")

    # ── 3. MA10触发清单 ──
    lines.append("")
    lines.append("MA10-4d 触发检查:")
    fired = 0
    for r in rows:
        c = str(r.get("code", "")).strip().zfill(6)
        name = r.get("name", "")
        try:
            cost = float(r.get("cost_price") or 0)
        except ValueError:
            cost = 0
        try:
            if c[:3] in CB_CODES:
                continue  # 转债不做MA10
            d = load_daily(c, "2026-03-01", today)
            if d.empty or len(d) < 15:
                continue
            d = d.sort_values("date")
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            ma10 = cl.rolling(10).mean()
            below = (cl < ma10)
            cons = 0
            for v in below.iloc[::-1]:
                if v:
                    cons += 1
                else:
                    break
            if cons < MA10_DAYS:
                continue
            cur = cl.iloc[-1]
            pnl_pct = (cur / cost - 1) * 100 if cost > 0 else None
            rsi = _rsi(cl)
            # 豁免判断
            exempt = []
            if rsi < 30:
                exempt.append(f"RSI{rsi:.1f}<30豁免，等回35")
            if pnl_pct is not None and pnl_pct > 50:
                exempt.append("浮盈>50%改MA20")
            fired += 1
            if exempt:
                lines.append(f"  ⏸ {name}({c}) 连破{cons}天 距MA10{(cur/ma10.iloc[-1]-1)*100:+.1f}%  → {', '.join(exempt)}")
            else:
                lines.append(f"  🔴 {name}({c}) 连破{cons}天 距MA10{(cur/ma10.iloc[-1]-1)*100:+.1f}% RSI{rsi:.1f} → 按纪律卖出（V反豁免盘中判）")
        except Exception:
            pass
    if fired == 0:
        lines.append("  无触发")

    # ── 4. 外盘跳空提示（只提示不操作） ──
    lines.append("")
    lines.append("外盘（只提示跳空，不给操作建议）:")
    try:
        from web.services.signal_alerts import get_holding_signal_alerts
        r = get_holding_signal_alerts()
        for s in r.get("signal_status", []):
            tag = "盘中" if s.get("live") else "收盘"
            lines.append(f"  {s['name']} {s['chg_pct']:+.2f}% ({tag} {s['date']})")
        if r.get("market_alert"):
            lines.append(f"  🚨 {r['market_alert']['advice']}")
    except Exception as e:
        lines.append(f"  外盘拉取失败: {str(e)[:60]}")

    report = "\n".join(lines)
    logger.info(report)
    print(report)

    # ── 5. 有异常才升级发邮件；正常也发简报 ──
    from monitoring.alerts import send_alert
    send_alert(report[:1500], level="warning" if problems else "info")


if __name__ == "__main__":
    main()
