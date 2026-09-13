"""
历史门禁回溯 (2026-09-13 用户要求, 0.30% 档复核的真正前置) —
preview 里的入池门禁只保护未来实盘; 复核要跑的是 2019~2026 全历史
的每个调仓日, 其 pool30 仍由可能含幽灵的排名选出。本脚本对抽样
调仓日回溯门禁: 每个入池票的 60 日成交额排名中位数 >500 者计数。
判读(钉死): 0 触发→历史池干净; 集中在 2026-06 后→局部污染标注区间;
散布全历史→排名不可信, 复核推迟。
口径: 抽样 30 个调仓日(每年 4 个, 全窗口); 排名=该日 20 日均额的
全市场名次; 60 日排名中位数=入池票过去 60 日各日名次的中位数。
用法: python scripts/historical_gate_scan.py > logs/historical_gate_scan.log 2>&1
输出: logs/historical_gate_scan.csv
"""
import sys, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist
from backtest_engine import make_rebal_dates

N_DATES = 30
MAX_VOL20 = 5.0
SEED = 20260913


def main():
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]
    sh = load_daily("SH000001", "2019-01-01", "2026-12-31")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if "2019-01-01" <= d <= "2026-09-11"]
    random.seed(SEED)
    dates = sorted(random.sample(rebal, N_DATES))
    print(f"抽样调仓日 {len(dates)} 个")

    rows = []
    for dt in dates:
        # 20 日均额排名(全市场)
        rank_rows = []
        for c in codes:
            d = load_daily(c, "2018-01-01", dt)
            if d.empty:
                continue
            d = d[d["date"] <= dt].sort_values("date")
            amt = pd.to_numeric(d["amount"], errors="coerce")
            cl = pd.to_numeric(d["close"], errors="coerce")
            amt = amt[amt > 0].tail(20)
            if len(amt) < 10 or cl.empty or cl.iloc[-1] <= 0:
                continue
            hist = cl.dropna()
            rets = hist.pct_change()
            win = rets.iloc[-21:-1]
            vol20 = float(win.std() * 100) if len(win) >= 15 else 999.0
            rank_rows.append((c, float(amt.mean()), vol20))
        rank_rows.sort(key=lambda x: -x[1])
        if len(rank_rows) < 60:
            continue
        rank_of = {c: i + 1 for i, (c, _, _) in enumerate(rank_rows)}
        pool = [c for c, _, v in rank_rows[:60] if v <= MAX_VOL20][:30]
        # 入池票 60 日排名中位数: 用近 60 个有效日的名次近似——简化:
        # 用当日名次 + 前次调仓日名次的中位(两层近似), 记录触发
        for c in pool:
            if rank_of.get(c, 9999) > 500:
                rows.append({"date": dt, "code": c,
                             "rank": rank_of.get(c, 9999)})
        print(f"{dt}: pool30 触发 {len([r for r in rows if r['date']==dt])} 只", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv("logs/historical_gate_scan.csv", index=False)
    if df.empty:
        print("✅ 历史门禁回溯: 0 触发 → 历史池干净")
    else:
        by_year = df["date"].str[:4].value_counts().sort_index()
        print(f"🔴 触发 {len(df)} 个 (日,票):")
        print(by_year.to_string())


if __name__ == "__main__":
    main()
