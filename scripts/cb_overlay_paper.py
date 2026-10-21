"""CB 腿趋势过滤前向纸面（2026-10-21 起，用户裁定“先验证再动”）。

信号（前向主口径）：CB 腿自身 NAV（V3 N10）的 MA200（实盘可直接算）。
数据：回测 nav_V3_N10（至 2026-09-18）+ 实盘 nav_cb（qmt_nav_history，逐快照日）
拼接；t−1 收盘信号 → t 日持有 CB 策略 / 现金。
纸面：ON=实盘 CB 腿当日收益；OFF=现金（CASH_YIELD/252）。每日追加
logs/cb_overlay_paper.csv；≥60 交易日后评估。
参考信号（不参与纸面）：CB 指数 MA200、nav_yi MA200。
用法：python scripts/cb_overlay_paper.py [--signal-only]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from run_backtest_a import CASH_YIELD

PAPER_START = "2026-10-21"
OUT = Path("logs/cb_overlay_paper.csv")
CASH = float((1 + CASH_YIELD) ** (1 / 252) - 1)


def combined_nav():
    nav = pd.read_csv("logs/cb_eligibility_arms_nav.csv",
                      index_col=0, parse_dates=True)["nav_V3_N10"]
    q = pd.read_parquet("logs/qmt_nav_history.parquet")
    q["date"] = pd.to_datetime(q["date"])
    q = q.set_index("date")
    live = q["nav_cb"].dropna()
    lr = live.pct_change().dropna()
    lr = lr[lr.index > nav.index[-1]]
    if len(lr):
        ext = (1 + lr).cumprod() * nav.iloc[-1]
        nav = pd.concat([nav, ext]).sort_index()
    return nav, live, lr


def main():
    signal_only = "--signal-only" in sys.argv
    nav, live, lr = combined_nav()
    ma = nav.rolling(200, min_periods=120).mean()
    sig = (nav > ma).astype(float)
    # 参考：CB 指数
    idx = pd.read_parquet("data_store/meta/cb_index.parquet")
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.set_index("date")["close"].astype(float).sort_index()
    idx_sig = (idx > idx.rolling(200, min_periods=120).mean()).astype(float)
    # 参考：nav_yi
    yi = pd.read_csv("logs/cb_eligibility_arms_nav.csv",
                     index_col=0, parse_dates=True)["nav_yi"]
    yi_sig = (yi > yi.rolling(200, min_periods=120).mean()).astype(float)
    print("latest", nav.index[-1].date(), "nav", round(float(nav.iloc[-1]), 4),
          "ma200", round(float(ma.iloc[-1]), 4),
          "SELF", int(sig.iloc[-1]),
          "| IDX", int(idx_sig.iloc[-1]) if len(idx_sig.dropna()) else None,
          "| YI", int(yi_sig.iloc[-1]))
    if signal_only:
        return

    last = live.index[-1]
    if last.strftime("%Y-%m-%d") < PAPER_START:
        print("paper not started (latest live", last.date(), ")")
        return
    cb_ret = float(lr.iloc[-1])
    prev_sig = float(sig.shift(1).iloc[-1])
    paper_ret = prev_sig * cb_ret + (1 - prev_sig) * CASH
    row = {"date": last.strftime("%Y-%m-%d"), "signal_prev": prev_sig,
           "signal_now": int(sig.iloc[-1]), "cb_ret": round(cb_ret, 6),
           "paper_ret": round(paper_ret, 6)}
    if OUT.exists():
        d = pd.read_csv(OUT)
        d = d[d["date"] != row["date"]]
        prev_nav = float((1 + d["paper_ret"]).prod()) if len(d) else 1.0
        row["paper_nav"] = round(prev_nav * (1 + paper_ret), 6)
        d = pd.concat([d, pd.DataFrame([row])], ignore_index=True)
    else:
        row["paper_nav"] = round(1 + paper_ret, 6)
        d = pd.DataFrame([row])
    d.to_csv(OUT, index=False)
    print("appended", row, "->", OUT)


if __name__ == "__main__":
    main()
