"""CB 腿夏普窗口扫描——追查 09-07 spec 手记 0.44 的出处口径（2026-09-24）。
读 logs/cb_doublelow_nav.csv（2026-09-22 15:02 跑, 现库 V3 口径）,
对候选窗口/频率重算夏普, 看哪个口径落点接近 0.44。
"""
import pandas as pd
import numpy as np

nav = pd.read_csv("logs/cb_doublelow_nav.csv", index_col=0)
nav.index = pd.to_datetime(nav.index)
cb = nav["cb"]


def shp_daily(s):
    r = s.pct_change().dropna()
    return r.mean() / r.std() * np.sqrt(252)


def shp_monthly(s):
    m = s.resample("ME").last().pct_change().dropna()
    return m.mean() / m.std() * np.sqrt(12)


wins = [
    ("全期2019.2~2026.9 日频", "2019-02-01", "2026-09-18", "daily"),
    ("2022~2026.8 日频", "2022-01-01", "2026-08-31", "daily"),
    ("2021~2026.8 日频", "2021-01-01", "2026-08-31", "daily"),
    ("2022~2026.8 月频", "2022-01-01", "2026-08-31", "monthly"),
    ("2019~2026.8 月频", "2019-01-01", "2026-08-31", "monthly"),
    ("2023~2026.8 月频", "2023-01-01", "2026-08-31", "monthly"),
    ("2024.7~2026.8 月频", "2024-07-01", "2026-08-31", "monthly"),
    ("2025~2026.8 日频", "2025-01-01", "2026-08-31", "daily"),
    ("2026.1~2026.8 日频", "2026-01-01", "2026-08-31", "daily"),
]
print(f"{'窗口':<26}{'夏普':>7}")
for name, lo, hi, freq in wins:
    s = cb[(cb.index >= lo) & (cb.index <= hi)]
    if len(s) < 100:
        print(f"{name:<26}  样本{len(s)}天跳过")
        continue
    fn = shp_monthly if freq == "monthly" else shp_daily
    print(f"{name:<26}{fn(s):>7.2f}")
