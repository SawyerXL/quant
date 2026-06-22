"""
手动持仓监测：紫金矿业/万泰生物/航发动力/汇川技术
每日收盘后检查 MA10 + 5日动量，买卖信号发邮件。

用法: python scripts/monitor_watchlist.py
Cron: 0 16 * * 1-5 (收盘后运行)
"""
import sys, json; from pathlib import Path
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
    "002559": "亚威股份",
    "300408": "三环集团",
}
# 个人成本价+持仓 (2026-06-22更新; 云南白药已卖出剔除)
MY_COST = {"600893": 32.799, "601899": 35.409, "603392": 63.157,
           "002559": 9.196, "300124": 74.019, "300408": 154.382}
MY_SHARES = {"600893": 700, "601899": 600, "603392": 1000,
             "002559": 3300, "300124": 200, "300408": 100}

MA_WINDOW = 10; EXIT_DAYS = 3


def check(code: str, name: str, today: str) -> dict:
    # 优先读盘中实时数据
    intra = Path("logs/intraday_watchlist.json")
    if intra.exists():
        try:
            d = json.loads(intra.read_text(encoding="utf-8"))
            updated=pd.Timestamp(d["updated"]); now=pd.Timestamp.now()
            if (now-updated).seconds<600:  # 10分钟内
                for s in d["stocks"]:
                    if s["code"]==code and "error" not in s:
                        return {"code":code,"name":name,"cur":s["cur"],"ma10":s["ma10"],
                                "ret_5d":s["ret_5d"],"below":s["below"],"signal":s["signal"],
                                "bounce":False,"bounce_drop":0,"source":"intraday"}
        except Exception: pass

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

    # 超跌反弹检测
    bounce = False; bounce_drop = 0
    if len(closes) >= 20:
        recent_high = max(closes[-30:]) if len(closes) >= 30 else max(closes)
        drop = (cur / recent_high - 1) * 100
        opens = df["open"].values; open_today = opens[-1] if len(opens) > 0 else cur
        if drop <= -15 and cur > open_today:
            bounce = True; bounce_drop = round(drop, 1)

    held = code in MY_COST
    if below >= EXIT_DAYS:      signal = "SELL" if held else "AVOID"
    elif cur > ma10 and ret_5d > 2: signal = "HOLD" if held else "STRONG"
    elif cur > ma10:            signal = "HOLD" if held else "OK"
    else:                       signal = "WATCH" if held else "WAIT"

    return {"code": code, "name": name, "cur": cur, "ma10": round(ma10, 2),
            "ret_5d": round(ret_5d, 1), "below": below, "signal": signal,
            "bounce": bounce, "bounce_drop": bounce_drop}


def main():
    today = date.today().strftime("%Y-%m-%d")
    results = [check(c, n, today) for c, n in WATCHLIST.items()]
    alerts = []
    for r in results:
        s = r["signal"]
        tag = {"SELL": "🔴卖出", "AVOID": "🚫避开", "STRONG": "🔥强势(可买)", "OK": "✅尚可", "HOLD": "✅继续持有",
               "WATCH": "⚠️观察(已有)", "WAIT": "⏳等待", "?": "❓无数据"}[s]
        pnl_extra = ""
        if r['code'] in MY_COST:
            pnl = (r.get('cur', 0) / MY_COST[r['code']] - 1) * 100
            pnl_extra = f"  个人盈亏{pnl:+.1f}% ({MY_SHARES[r['code']]}股@{MY_COST[r['code']]:.2f})"
        meanings={"SELL":"MA10跌破3天","AVOID":"趋势走坏","STRONG":"强势可关注","OK":"趋势尚可",
                  "HOLD":"持有中,走势好","WATCH":"MA10边缘持有中","WAIT":"等方向明确","?":""}
        actions={"SELL":"明天开盘卖出","AVOID":"","STRONG":"可考虑买入","OK":"",
                 "HOLD":"","WATCH":"等收复MA10","WAIT":"","?":""}
        print(f"  {r['code']} {r['name']:<8} 现价{r.get('cur',0):.2f} "
              f"MA10={r.get('ma10',0):.2f} {r.get('ret_5d',0):+.1f}%  {tag:<14} {meanings.get(s,''):<12} {actions.get(s,'')}{pnl_extra}")
        if s in ("SELL",):
            alerts.append(f"🔴卖出 {r['code']} {r['name']} MA10={r.get('ma10',0):.2f}")
        if s in ("STRONG",) and r['code'] not in MY_COST:
            alerts.append(f"🔥可关注 {r['code']} {r['name']} 5日{r.get('ret_5d',0):+.1f}%")
        if r.get("bounce"):
            print(f"     👀 超跌反弹: 从高点跌{r['bounce_drop']}%后收阳，值得关注")
            alerts.append(f"👀超跌反弹 {r['code']} {r['name']} "
                          f"跌{r['bounce_drop']}%后收阳，可以考虑抄底")

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
