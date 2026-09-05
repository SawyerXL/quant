"""
IC 预检三查（2026-09-05，立项前的红旗流程——IC -0.097 强得可疑, 先排假强）。

① 两信号相关性: vol20 rank 与 成交额动量 rank 的逐期 Spearman 平均
   (>0.7=一个信号两种写法, 只用vol20; <0.5=两个正交信号)
② 残差 IC: vol20 rank 对 成交额排名 rank 做截面回归取残差, 残差 IC
   仍显著负=独立alpha; 消失=只是"池子里越热越差"的另一种写法
③ TOP60 子集 IC: 策略实际吃的截面(最热的60只)里 vol20 是否仍有排序力
口径与 ic_framework 一致: TOP300池, 双周切面, T-1严格, fwd=10日。
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

    corrs, ics_full, ics_resid, ics_60 = [], [], [], []
    for d in rebal:
        i = pos.get(d)
        if i is None or i < 30 or i + 10 >= len(dates):
            continue
        uni = amt20.iloc[i - 1].nlargest(TOPN).index.tolist()
        uni = [c for c in uni if c in panel.columns]
        vol20 = rets.iloc[i - 20:i][uni].std() * np.sqrt(252)
        amt_chg = (amt20.iloc[i - 1][uni] / amt_prev.iloc[i - 1][uni] - 1)
        amt_rank = amt20.iloc[i - 1][uni].rank()
        fwd = panel.iloc[i + 10][uni] / panel.iloc[i][uni] - 1
        df = pd.DataFrame({"vol20": vol20, "amt_chg": amt_chg,
                           "amt_rank": amt_rank, "fwd": fwd}).dropna()
        if len(df) < 50:
            continue
        v_r = df["vol20"].rank()
        a_r = df["amt_chg"].rank()
        corrs.append(v_r.corr(a_r, method="spearman"))
        ics_full.append(v_r.corr(df["fwd"].rank(), method="spearman"))
        # 残差: vol20 rank ~ amt_rank rank
        x = df["amt_rank"].rank().values
        y = v_r.values
        b = np.polyfit(x, y, 1)
        resid = y - np.polyval(b, x)
        # 索引对齐: resid重建Series时必须带回df的股票代码索引, 否则corr错位
        r_resid = pd.Series(resid, index=df.index).rank()
        ics_resid.append(r_resid.corr(df["fwd"].rank(), method="spearman"))
        # TOP60子集
        top60 = df.nlargest(60, "amt_rank")
        if len(top60) >= 30:
            ics_60.append(top60["vol20"].rank().corr(
                top60["fwd"].rank(), method="spearman"))

    corrs = np.array(corrs)
    ics_full = np.array(ics_full)
    ics_resid = np.array(ics_resid, dtype=float)
    ics_60 = np.array(ics_60, dtype=float)
    n_resid_nan = int(np.isnan(ics_resid).sum())
    print(f"\n采样点: {len(corrs)}个双周切面 (残差IC中NaN {n_resid_nan}个)", flush=True)
    print(f"① vol20与成交额动量 rank相关: 均值 {corrs.mean():+.3f} "
          f"(最小{corrs.min():+.3f} 最大{corrs.max():+.3f})", flush=True)
    print(f"② vol20 全池 IC: {ics_full.mean():+.4f} (t={ics_full.mean()/(ics_full.std()/np.sqrt(len(ics_full))):+.1f}) | "
          f"残差IC(剔成交额排名): {np.nanmean(ics_resid):+.4f} "
          f"(t={np.nanmean(ics_resid)/(np.nanstd(ics_resid)/np.sqrt(np.isfinite(ics_resid).sum())):+.1f})", flush=True)
    print(f"③ TOP60子集 vol20 IC: {ics_60.mean():+.4f} "
          f"(t={ics_60.mean()/(ics_60.std()/np.sqrt(len(ics_60))):+.1f}, "
          f"n={len(ics_60)})", flush=True)


if __name__ == "__main__":
    main()
