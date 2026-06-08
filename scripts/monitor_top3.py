"""
手动跟单三只最强股：亨通光电(600487)、大唐发电(601991)、横店东磁(002056)
每日收盘后自动评估买卖信号。

信号规则（基于策略已验证逻辑）：
  🔴 卖出：连续3天收盘低于MA10（强制止损）
  🟡 减持：浮盈>15% 且 MA10拐头向下（高位获利减仓）
  🟢 买入/加仓：MA10之上 且 5日动量转正 且 波动率不失控
  ⚪ 持有：其余情况

运行：python scripts/monitor_top3.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from loguru import logger
from data.storage import load_daily

logger.add("logs/monitor_top3.log", rotation="1 week")

TARGETS = {
    "600487": {"name": "亨通光电", "entry_price": None, "notes": "新策略首推，通信龙头"},
    "601991": {"name": "大唐发电", "entry_price": 7.71,   "notes": "老兵+14%，公用事业"},
    "002056": {"name": "横店东磁", "entry_price": 24.52,  "notes": "老兵+20%，电力设备稀土"},
}

MA_WINDOW    = 10
EXIT_DAYS    = 3    # 连续跌破N天出清
TAKE_PROFIT  = 0.15 # 浮盈15%以上开始关注减持


def analyze(code: str, name: str, entry: float | None) -> dict:
    """分析单只股票，返回信号字典。"""
    end   = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    df = load_daily(code, start, end)

    if df.empty or len(df) < 10:
        return {"code": code, "name": name, "signal": "⚪ 数据不足", "action": "hold"}

    df = df.sort_values("date")
    closes = df["close"].values
    cur    = closes[-1]
    ma10   = closes[-MA_WINDOW:].mean()
    vol_20 = pd.to_numeric(df["amount"], errors="coerce").tail(20).mean()

    # ── MA10 状态 ──────────────────────────────────────
    days_below = 0
    for c in reversed(closes):
        if c < ma10: days_below += 1
        else:        break

    # ── 动量 ───────────────────────────────────────────
    ret_5d  = (cur / closes[-6]  - 1) * 100 if len(closes) >= 6  else 0
    ret_20d = (cur / closes[-21] - 1) * 100 if len(closes) >= 21 else 0

    # MA10方向（过去5个MA10的斜率）
    mas = [closes[max(0,i-MA_WINDOW):i].mean() for i in range(max(0,len(closes)-5), len(closes)+1)]
    mas = [m for m in mas if not np.isnan(m)]
    ma_slope = (mas[-1] / mas[0] - 1) * 100 if len(mas) >= 2 and mas[0] > 0 else 0

    # ── 波动率 ─────────────────────────────────────────
    rets = pd.Series(closes).pct_change().dropna()
    vol = rets.tail(20).std() * np.sqrt(252) if len(rets) >= 20 else 0.5

    # ── 盈亏 ───────────────────────────────────────────
    pnl = (cur / entry - 1) * 100 if entry else None

    # ── 判定信号 ───────────────────────────────────────
    if days_below >= EXIT_DAYS:
        signal = f"🔴 卖出（连续{days_below}天低于MA{MA_WINDOW}，强制止损）"
        action = "sell"
    elif entry and pnl and pnl > TAKE_PROFIT * 100 and ma_slope < -1:
        signal = f"🟡 减持（浮盈{pnl:.0f}%已达目标，MA{MA_WINDOW}拐头向下）"
        action = "reduce"
    elif cur > ma10 and ret_5d > 0 and vol < 0.60:
        signal = f"🟢 可加仓（MA{MA_WINDOW}之上，动量转正，波动可控）"
        action = "buy"
    elif cur > ma10:
        signal = f"🟢 持有偏多（MA{MA_WINDOW}之上，趋势良好）"
        action = "hold_bull"
    else:
        signal = f"⚪ 观望（MA{MA_WINDOW}之下但未触发止损，等待确认）"
        action = "hold"

    return {
        "code": code, "name": name, "signal": signal, "action": action,
        "cur": cur, "ma10": round(ma10, 2),
        "days_below_ma10": days_below,
        "ret_5d": round(ret_5d, 1), "ret_20d": round(ret_20d, 1),
        "vol": round(vol, 2), "ma_slope": round(ma_slope, 1),
        "pnl": round(pnl, 1) if pnl else None,
        "volume_avg": f"{vol_20/10000:.0f}万" if vol_20 else "?",
    }


def main():
    today = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  Top 3 手动跟单信号  {today}")
    print(f"{'='*60}")

    results = []
    for code, info in TARGETS.items():
        r = analyze(code, info["name"], info["entry_price"])
        results.append(r)

        pnl_str = f"浮盈{r['pnl']:+.1f}%" if r['pnl'] is not None else "无入场价"
        print(f"\n  {r['code']} {r['name']}")
        print(f"    {r['signal']}")
        print(f"    收盘: {r['cur']:.2f}  MA10: {r['ma10']}  "
              f"低于MA: {r['days_below_ma10']}天  {pnl_str}")
        print(f"    5日: {r['ret_5d']:+.1f}%  20日: {r['ret_20d']:+.1f}%  "
              f"波动率: {r['vol']:.0%}  MA10方向: {r['ma_slope']:+.1f}%")
        print(f"    备注: {info['notes']}")

    # ── 汇总 ───────────────────────────────────────────
    actions = {"buy": [], "sell": [], "reduce": [], "hold": [], "hold_bull": []}
    for r in results:
        actions[r["action"]].append(r["code"])

    print(f"\n  {'─'*60}")
    if actions["sell"]:
        print(f"  🚨 今日需操作(卖): {', '.join(actions['sell'])}")
    if actions["reduce"]:
        print(f"  ⚠️  建议减仓: {', '.join(actions['reduce'])}")
    if actions["buy"]:
        print(f"  💰 可加仓: {', '.join(actions['buy'])}")
    if not actions["sell"] and not actions["reduce"] and not actions["buy"]:
        print(f"  今日无操作信号，全部持有/观望")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    main()
