"""方向台账 auto-base：MA200 条件基准率的纯函数口径。"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import prediction_ledger as pl


def test_base_rate_below_and_above_h1():
    idx = pd.bdate_range("2020-01-01", periods=5)
    s = pd.Series([100, 101, 102, 103, 104], index=idx, dtype=float)
    dist = pd.Series([0.1, -0.1, 0.1, -0.1, 0.1], index=idx)
    p, n = pl.base_rate(s, dist, 1, True)
    assert n == 2 and p == 1.0
    p2, n2 = pl.base_rate(s, dist, 1, False)
    assert n2 == 2 and p2 == 1.0


def test_base_rate_h5():
    idx = pd.bdate_range("2020-01-01", periods=7)
    s = pd.Series([100, 101, 102, 103, 104, 105, 106], index=idx, dtype=float)
    dist = pd.Series([-0.1] * 7, index=idx)
    p, n = pl.base_rate(s, dist, 5, True)
    assert n == 2 and p == 1.0   # 只有前两日有 5 日后收盘
