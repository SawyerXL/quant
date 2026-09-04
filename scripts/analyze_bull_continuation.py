"""
牛市延续性分析（2026-09-04，用户问题：这轮牛市还能继续吗/4000点还能上吗）。

口径写明:
  - 样本: 上证日线 1991-2026（本地库, ~8500交易日）
  - 信号时点: T-1及以前数据定义状态, T日起计未来收益(无未来函数)
  - 不涉及成本/交易, 纯指数条件统计
  - 小样本结论必须标注样本量, 不写铁律(回测复验纪律第3条)
当前画像(9/4): 上证3966, 距MA200约-3.1%(ratio 0.969), 距4000点+0.86%。
分析:
  Q1 4000点触及: 全历史'距上方0.9%目标'的摸到天数分布 + 条件于当前状态
  Q2 牛市延续: 当前状态(MA200下方且距离<5%, ratio∈[0.95,1.0))的历史同
     状态样本, 未来20/60/120日收益分布 vs 无条件基准
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily


def main():
    d = load_daily("000001", "1991-01-01", "2026-09-04")
    d = d.sort_values("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    dates = pd.to_datetime(d["date"])
    s = pd.Series(cl.values, index=dates).dropna()
    r = s.pct_change().dropna()

    ma200 = s.rolling(200).mean()
    ratio = s / ma200
    vol20 = r.rolling(20).std() * np.sqrt(252)
    ret20 = s.pct_change(20)

    print("=== 当前画像 ===")
    print(f"上证 {s.iloc[-1]:.0f} | MA200 {ma200.iloc[-1]:.0f} "
          f"| ratio {ratio.iloc[-1]:.4f} | 前20日波动率 {vol20.iloc[-1]*100:.1f}% "
          f"| 前20日涨幅 {ret20.iloc[-1]*100:+.1f}%")

    # ── Q1: 距上方0.9%目标的历史触及统计 ──
    # 对每个历史日: 未来60日内是否摸到 close >= 当日×(1+0.0086)
    hits, days = [], []
    for i in range(len(s) - 60):
        tgt = s.iloc[i] * 1.0086
        fut = s.iloc[i + 1:i + 61]
        hit = fut[fut >= tgt]
        if len(hit):
            hits.append(1)
            days.append(fut.index.get_loc(hit.index[0]) + 1)
        else:
            hits.append(0)
            days.append(np.nan)
    hit_arr = np.array(hits); day_arr = np.array(days)
    print("\n=== Q1: 距上方+0.86%目标(≈4000点)的历史触及 ===")
    print(f"无条件: 60日内摸到概率 {hit_arr.mean()*100:.0f}% "
          f"(n={len(hit_arr)}), 摸到时中位天数 {np.nanmedian(day_arr):.0f}")
    for win in (5, 10, 20):
        p = np.nanmean(day_arr <= win)
        print(f"  {win}日内摸到概率: {p*100:.0f}%")

    # 条件于当前状态: MA200下方但距离<5%
    cond = (ratio > 0.95) & (ratio < 1.0)
    idx_ok = cond.shift(1).fillna(False).values
    for win in (5, 10, 20, 60):
        cnt = 0; hitn = 0
        for i in np.where(idx_ok)[0]:
            if i >= len(s) - win - 1:
                continue
            tgt = s.iloc[i] * 1.0086
            if (s.iloc[i+1:i+win+1] >= tgt).any():
                hitn += 1
            cnt += 1
        if cnt > 10:
            print(f"  条件(MA200下方<5%, n={cnt}): {win}日内摸到 {hitn/cnt*100:.0f}%")

    # ── Q2: 牛市延续条件统计 ──
    print("\n=== Q2: 当前状态(ratio∈[0.95,1.0))的未来收益分布 ===")
    for horizon in (20, 60, 120):
        fwd = []
        for i in np.where(idx_ok)[0]:
            if i + horizon < len(s):
                fwd.append(s.iloc[i+horizon] / s.iloc[i] - 1)
        base = []
        for i in range(200, len(s) - horizon, 20):   # 无条件抽样(每20日取一点防重叠)
            base.append(s.iloc[i+horizon] / s.iloc[i] - 1)
        fwd = np.array(fwd); base = np.array(base)
        print(f"  {horizon}日: 条件均值{fwd.mean()*100:+.1f}% "
              f"胜率{(fwd>0).mean()*100:.0f}% 最差{fwd.min()*100:.1f}% "
              f"最好{fwd.max()*100:.1f}% (n={len(fwd)}) | "
              f"无条件均值{base.mean()*100:+.1f}% 胜率{(base>0).mean()*100:.0f}%")
    # 次状态: ratio>1.0 (MA200上方) 对照
    above = (ratio > 1.0).shift(1).fillna(False).values
    for horizon in (20, 60, 120):
        fwd = [s.iloc[i+horizon]/s.iloc[i]-1 for i in np.where(above)[0]
               if i + horizon < len(s)]
        fwd = np.array(fwd)
        print(f"  对照(MA200上方) {horizon}日: 均值{fwd.mean()*100:+.1f}% "
              f"胜率{(fwd>0).mean()*100:.0f}% (n={len(fwd)})")


if __name__ == "__main__":
    main()
