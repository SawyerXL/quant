"""
手动持仓监测：紫金矿业/万泰生物/航发动力/汇川技术
每日收盘后检查 MA10 + 5日动量，买卖信号发邮件。

用法: python scripts/monitor_watchlist.py
Cron: 0 16 * * 1-5 (收盘后运行)
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from datetime import date
from loguru import logger
from data.storage import load_daily

WATCHLIST = {
    "601899": "紫金矿业",
    "603392": "万泰生物",
    "600893": "航发动力",
    "300124": "汇川技术",
}

MA_WINDOW = 10; EXIT_DAYS = 3


def check(code: str, name: str, today: str) -> dict:
    df = load_daily(code, (date.today() - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), today)
    if df.empty or len(df) < 10:
        return {"code": code, "name": name, "signal": "?"}
    df = df.sort_values("date")
    closes = df["close"].values; cur = closes[-1]
    ma10 = closes[-MA_WINDOW:].mean() if len(closes) >= MA_WINDOW else cur
    ret_5d = (cur / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
    below = 0
    for c in reversed(closes):
        if c < ma10: below += 1
        else: break

    if below >= EXIT_DAYS:      signal = "SELL"
    elif cur > ma10 and ret_5d > 2: signal = "BUY"
    elif cur > ma10:            signal = "HOLD"
    else:                       signal = "WAIT"

    return {"code": code, "name": name, "cur": cur, "ma10": round(ma10, 2),
            "ret_5d": round(ret_5d, 1), "below": below, "signal": signal}


def main():
    today = date.today().strftime("%Y-%m-%d")
    results = [check(c, n, today) for c, n in WATCHLIST.items()]
    alerts = []
    for r in results:
        s = r["signal"]
        tag = {"SELL": "🔴卖出", "BUY": "🟢买入", "HOLD": "✅持有", "WAIT": "⚠️观望", "?": "❓无数据"}[s]
        print(f"  {r['code']} {r['name']:<8} 现价{r.get('cur',0):.2f} "
              f"MA10={r.get('ma10',0):.2f} {r.get('ret_5d',0):+.1f}%  {tag}")
        if s in ("SELL", "BUY"):
            alerts.append(f"{tag} {r['code']} {r['name']} "
                          f"MA10={r.get('ma10',0):.2f} 5日={r.get('ret_5d',0):+.1f}%")

    if alerts:
        try:
            from monitoring.alerts import send_alert
            send_alert("【持仓监测】" + " | ".join(alerts))
        except Exception as e:
            logger.warning(f"告警失败: {e}")
    else:
        print("  无买卖信号")


if __name__ == "__main__":
    print(f"\n  {'='*45}")
    print(f"  持仓监测  {date.today()}")
    print(f"  {'='*45}")
    main()
    print()
