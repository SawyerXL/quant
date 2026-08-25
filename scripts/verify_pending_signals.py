"""
复验两个"待验证"信号，口径严格按原框架定义（不是我自己发明的版本）：

A. 恒生→当晚美股（原称"最强领先信号"，3015天样本）
   链：港股16:00收盘 → 美股21:30开盘，同日D。恒生D跌幅 → 美股D收盘表现。
   注意：美股日线bar标D（美东日期），恒生bar标D（北京时间）—— 同一D，因为
   北京D的16:00收盘后美股当晚(美东D)才开盘。用 ak.stock_hk_index_daily_sina。

B. 恐慌底+缩量（原定义：恐慌日(>-3%)次日缩量<80%恐慌量 → 抄底）
   上证指数 volume 列。入场=缩量日收盘，看5/10/20日forward收益与胜率。
   对照：恐慌日无缩量。

用法: python scripts/verify_pending_signals.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 else np.nan


def sec(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def sigA():
    sec("A. 恒生当日 → 当晚美股（原'最强领先信号'）")
    hsi = ak.stock_hk_index_daily_sina(symbol="HSI").sort_values("date")
    us = {"SOX": ak.index_us_stock_sina(symbol=".SOX"),
          "S&P500": ak.index_us_stock_sina(symbol=".INX")}
    hsi["date"] = pd.to_datetime(hsi["date"])
    hsi = hsi.set_index("date")["close"].astype(float)
    hsi_ret = hsi.pct_change() * 100
    for name, df in us.items():
        df = df.sort_values("date")
        df["date"] = pd.to_datetime(df["date"])
        us_ret = df.set_index("date")["close"].astype(float).pct_change() * 100
        j = pd.DataFrame({"hsi": hsi_ret, "us": us_ret}).dropna()
        j["us"] = j["us"].reindex(j.index)  # 同日对齐: 北京D收盘→美东D盘
        print(f"\n  恒生 → {name}  样本{len(j)}天 {j.index[0].date()}→{j.index[-1].date()}")
        print(f"  无条件: {name}当晚 {j['us'].mean():+.3f}% (n={len(j)})")
        print(f"  {'恒生区间':<14}{'次数':>6}{'当晚均值':>10}{'胜率':>8}{'t值':>8}{'vs无条件':>10}")
        for lo, lab in [(-1, "跌>1%"), (-2, "跌>2%"), (-3, "跌>3%"),
                        (1, "涨>1%"), (2, "涨>2%")]:
            m = j["hsi"] <= lo if lo < 0 else j["hsi"] >= lo
            sub = j["us"][m]
            if len(sub) < 10:
                continue
            print(f"  {lab:<14}{len(sub):>6}{sub.mean():>9.3f}%"
                  f"{(sub > 0).mean() * 100:>7.1f}%{tstat(sub):>8.2f}"
                  f"{sub.mean() - j['us'].mean():>+9.3f}pp")
        # 单调性检查: 跌幅越狠, 当晚越弱?
        for lo in (-1, -2, -3):
            sub = j["us"][j["hsi"] <= lo]
            if len(sub) >= 10:
                print(f"    [跌>{abs(lo)}% 细看] {len(sub)}次, 均值{sub.mean():+.3f}%")


def sigB():
    sec("B. 恐慌底+缩量（原定义复现）")
    d = load_daily("000001", "2012-01-01", "2026-08-25").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    vol = pd.to_numeric(d["volume"], errors="coerce")
    f = pd.DataFrame({"close": cl, "vol": vol}).dropna()
    f["ret"] = f["close"].pct_change() * 100

    panic = f["ret"] <= -3.0
    nxt_shrink = ((f["vol"] / f["vol"].shift(1)) < 0.8).shift(-1).fillna(False).astype(bool)
    signal = (panic & nxt_shrink)   # 恐慌日, 且次日缩量<80%
    ctrl = (panic & ~nxt_shrink)    # 恐慌日但次日没缩量

    fwd = {n: (f["close"].shift(-n) / f["close"] - 1) * 100 for n in (1, 5, 10, 20)}
    print(f"  样本期 {f.index[0].date()}→{f.index[-1].date()}, {len(f)}天")
    print(f"  恐慌日(≤-3%): {panic.sum()}天")
    print(f"\n  {'情形':<26}{'次数':>5}{'1日':>8}{'5日':>8}{'10日':>8}{'20日':>9}{'20日胜率':>9}")
    for name, m in [("恐慌+次日缩量<80% (信号)", signal), ("恐慌+次日未缩量 (对照)", ctrl)]:
        if m.sum() == 0:
            print(f"  {name:<26}{0:>5}   无样本")
            continue
        vals = [f"{fwd[n][m].mean():+.2f}%" for n in (1, 5, 10, 20)]
        wr = (fwd[20][m] > 0).mean() * 100
        print(f"  {name:<26}{m.sum():>5}" + "".join(f"{v:>8}" for v in vals) + f"{wr:>8.1f}%")

    # 原框架还claim了"最近6次→1月后100%正收益"，1月=20交易日，直接列出最近6次
    print(f"\n  最近6次'恐慌+次日缩量'逐次明细（20日后收益）:")
    if signal.sum():
        for i, (dt, r20) in enumerate(zip(fwd[20][signal].index, fwd[20][signal]), 1):
            print(f"    {i}. {dt.date()} → 20日后 {r20:+.2f}%  {'✅' if r20 > 0 else '❌'}")
    # 也看看所有信号日的分布
    if signal.sum():
        all20 = fwd[20][signal].dropna()
        print(f"\n  全部{len(all20)}次: 20日均值{all20.mean():+.2f}%, 胜率{(all20>0).mean()*100:.1f}%, "
              f"最差{all20.min():+.2f}%({all20.idxmin().date()}), 最好{all20.max():+.2f}%")


if __name__ == "__main__":
    sigA()
    sigB()
