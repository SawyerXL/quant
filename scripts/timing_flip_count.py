"""
择时层换手成本量化（2026-09-14，登记文档 §8.4 本周首选，纯计数非回测）。

方法（用户裁定口径）：数五档择时的翻转次数 → ×每次换手 × 成本档 →
择时层成本拖累（0.13% 与 0.30% 各一）。输入 = 官方指数序列
csi800_index（干净数据，无污染面）——本脚本只产信号统计与成本算术，
不产策略收益。
两口径并报（2026-09-14 读码发现二者不同，差异本身=live-vs-backtest
实现差）：
  A 实盘口径: run_backtest_a2.get_position_ratio——纯函数 pos=f(ratio),
    无状态机无确认无滞后(实盘信号链 daily_signal_a_grouped 用此)
  B 引擎口径: backtest_engine Step0b 状态机(降档即时/升档确认关)
用法: python scripts/timing_flip_count.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_meta

TIER_POS = [0.30, 0.50, 0.70, 0.85, 1.00]
TIER_LOW = [None, 0.95, 0.98, 1.02, 1.05]
TIER_UP = [0.95, 0.98, 1.02, 1.05, None]
SHIFT = -0.03          # MA200_THRESH_SHIFT 2026-09-04 定案
COSTS = [0.0013, 0.0030]


def tier_of(r):
    if r >= 1.05: return 4
    if r >= 1.02: return 3
    if r >= 0.98: return 2
    if r >= 0.95: return 1
    return 0


def live_tiers(ratio_all):
    """实盘口径: 纯函数每日独立映射"""
    return [(d, tier_of(r)) for d, r in ratio_all.items()]


def engine_tiers(ratio_all):
    """引擎口径: 状态机(降档即时, 升档即时 conf=0)"""
    tier, out = 4, []
    for d, r in ratio_all.items():
        if TIER_LOW[tier] is not None and r < TIER_LOW[tier]:
            new_t = tier_of(r)
            if new_t < tier:
                tier = new_t
        if TIER_UP[tier] is not None and r >= TIER_UP[tier]:
            new_t = tier_of(r)
            if new_t > tier:
                tier = new_t
        out.append((d, tier))
    return out


def summarize(name, tiers):
    df = pd.DataFrame(tiers, columns=["date", "tier"])
    df["pos"] = df["tier"].map(lambda t: TIER_POS[t])
    years = (df["date"].max() - df["date"].min()).days / 365.25
    n_sw = int((df["tier"].diff() != 0).sum())
    to = float(df["pos"].diff().abs().sum() * 2)
    print(f"\n[{name}] 窗口 {df['date'].min().date()} ~ {df['date'].max().date()} "
          f"({years:.1f}年, {len(df)}日)")
    print(f"  翻转 {n_sw} 次, 年均 {n_sw/years:.1f} 次/年, "
          f"年均换手 {to/years*100:.0f}%/年")
    for c in COSTS:
        print(f"  成本档 {c*10000:.0f}bp → 择时层成本拖累 = {to/years*c*100:.2f}pp/年")
    # 各年分布(识别时变)
    df["year"] = df["date"].dt.year
    per_year = df.groupby("year")["tier"].agg(
        n_sw=lambda s: int((s.diff() != 0).sum()))
    print("  各年翻转:", dict(per_year))


def hysteresis_tiers(ratio_all, band):
    """滞后带变体: 升档需 ratio ≥ 上界+band(与引擎 ma200_hysteresis 同语义)"""
    tier, out = 4, []
    for d, r in ratio_all.items():
        if TIER_LOW[tier] is not None and r < TIER_LOW[tier]:
            new_t = tier_of(r)
            if new_t < tier:
                tier = new_t
        if TIER_UP[tier] is not None and r >= TIER_UP[tier] + band:
            new_t = tier_of(r)
            if new_t > tier:
                tier = new_t
        out.append((d, tier))
    return out


def continuous_tiers(ratio_all):
    """连续仓位变体: pos = clip(ratio-0.95, 0, 0.10)/0.10 映射 30%~100%"""
    out = []
    for d, r in ratio_all.items():
        pos = 0.30 + min(max(r - 0.95, 0.0), 0.10) / 0.10 * 0.70
        out.append((d, pos))
    return out


def summarize_continuous(name, tiers):
    df = pd.DataFrame(tiers, columns=["date", "pos"])
    years = (df["date"].max() - df["date"].min()).days / 365.25
    to = float(df["pos"].diff().abs().sum() * 2)
    print(f"\n[{name}] 窗口 {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(f"  年均换手 {to/years*100:.0f}%/年")
    for c in COSTS:
        print(f"  成本档 {c*10000:.0f}bp → 择时层成本拖累 = {to/years*c*100:.2f}pp/年")


def main():
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    ic = ic[ic.index >= "2019-01-01"]
    hist = ic[ic.index <= "2026-08-28"]
    ratio_all = (hist / hist.rolling(200).mean() + SHIFT).dropna()
    summarize("现网(纯函数五档)", live_tiers(ratio_all))
    summarize("变体: 升档滞后带 0.02", hysteresis_tiers(ratio_all, 0.02))
    summarize("变体: 升档滞后带 0.04", hysteresis_tiers(ratio_all, 0.04))
    summarize_continuous("变体: 连续仓位(0.95→1.05 线性 30%→100%)",
                         continuous_tiers(ratio_all))


if __name__ == "__main__":
    main()
