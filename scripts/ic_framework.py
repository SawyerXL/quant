"""
IC 框架（2026-09-05，专家评审立项 #25）——让选股维度开始工作。

第一步不是加因子, 是建测量: 成交额TOP300池内, 双周 rank IC + IC-IR,
不跑完整回测。候选(与现有机制正交):
  1. vol20    20日波动率(拥挤度过滤的连续版, 严格T-1口径)
  2. rev5     5日反转(A股最稳的横截面信号, 与MA10退出方向正交)
  3. amt_chg  成交额动量(20日均成交额/前20日-1, 拥挤中的相对冷热;
              流通股本缺失, 以成交额代理换手率变化)
口径: 因子T日(用≤T-1数据), 前向收益=close[T+10]/close[T]-1(双周),
      采样=双周调仓日历; 2019-2026.8。
采纳线(专家): IC-IR>0.3 且分年稳定; IC 0.02~0.03即够用(广度法则)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_engine import make_rebal_dates

START, END = "2019-01-01", "2026-08-28"
TOPN = 300


def main():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只", flush=True)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    dates = panel.index
    pos = {d: dates.get_loc(pd.Timestamp(d)) for d in rebal
           if pd.Timestamp(d) in dates}

    rets = panel.pct_change()
    amt20 = ap.rolling(20).mean()
    amt_prev = ap.rolling(20).mean().shift(20)

    results = {f: [] for f in ("vol20", "rev5", "amt_chg")}
    ic_rows = []
    for d in rebal:
        i = pos.get(d)
        if i is None or i < 30 or i + 10 >= len(dates):
            continue
        # TOP300 by 20日均成交额(T-1严格)
        amt_rank = amt20.iloc[i - 1]
        uni = amt_rank.nlargest(TOPN).index.tolist()
        uni = [c for c in uni if c in panel.columns]
        # 因子(T-1严格)
        vol20 = rets.iloc[i - 20:i][uni].std() * np.sqrt(252)
        rev5 = -(panel.iloc[i - 1][uni] / panel.iloc[i - 6][uni] - 1)
        amt_chg = (amt20.iloc[i - 1][uni] / amt_prev.iloc[i - 1][uni] - 1)
        # 前向10日收益
        fwd = panel.iloc[i + 10][uni] / panel.iloc[i][uni] - 1
        df = pd.DataFrame({
            "vol20": vol20, "rev5": rev5, "amt_chg": amt_chg, "fwd": fwd,
        }).dropna()
        if len(df) < 50:
            continue
        for f in ("vol20", "rev5", "amt_chg"):
            ic = df[f].corr(df["fwd"], method="spearman")
            results[f].append((d, ic))
        ic_rows.append({"date": d, "n": len(df),
                        "ic_vol20": df["vol20"].corr(df["fwd"], method="spearman"),
                        "ic_rev5": df["rev5"].corr(df["fwd"], method="spearman"),
                        "ic_amt": df["amt_chg"].corr(df["fwd"], method="spearman")})

    print(f"\n采样点: {len(ic_rows)}个双周切面, 平均池深~{TOPN}只\n", flush=True)
    for f, name in (("vol20", "20日波动率(连续版)"),
                    ("rev5", "5日反转"),
                    ("amt_chg", "成交额动量")):
        ics = np.array([x[1] for x in results[f]])
        ir = ics.mean() / ics.std() if ics.std() > 0 else 0
        t = ics.mean() / (ics.std() / np.sqrt(len(ics))) if ics.std() > 0 else 0
        print(f"{name:<20}: 均值IC {ics.mean():+.4f}  IC-IR {ir:+.2f}  "
              f"t={t:+.1f}  胜率(IC>0) {(ics>0).mean()*100:.0f}%  n={len(ics)}",
              flush=True)
        # 分年稳定
        by_year = pd.Series(ics, index=[pd.Timestamp(x[0]).year for x in results[f]])
        yr = by_year.groupby(by_year.index).mean()
        print(f"    分年IC: " + " ".join(f"{y}:{v:+.3f}" for y, v in yr.items()),
              flush=True)


if __name__ == "__main__":
    main()
