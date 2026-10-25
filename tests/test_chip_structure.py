"""筹码/洗盘模块单测（2026-10-25）。

覆盖：筹码分布基本性质、换手口径、状态机边界、ETF 识别。
不触网：analyze() 的网络/数据依赖不在单测范围，状态机已抽成纯函数。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import chip_structure as cs


def make_bars(closes, vol=1000.0):
    n = len(closes)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [vol] * n,
        "amount": [c * vol for c in closes],
    })


def test_chip_distribution_properties():
    rng = np.random.default_rng(7)
    closes = list(10 + np.cumsum(rng.normal(0, 0.15, 150)))
    d = make_bars(closes)
    chip = cs.chip_distribution(d)
    assert chip is not None
    assert chip["cost_5"] < chip["cost_50"] < chip["cost_95"]
    assert 0.0 <= chip["winner_rate"] <= 1.0
    assert chip["width90"] > 0
    assert chip["vwap_ok_ratio"] == 1.0
    assert chip["chip_window"] == cs.P["chip_window"]  # 只取回看窗口


def test_chip_distribution_empty_guard():
    d = make_bars([10.0] * 10)  # 少于 20 根 → None
    assert cs.chip_distribution(d) is None


def test_cum_turnover():
    # volume 1000 万股 = 1e7 股；流通 1e9 股 → 每日 1%；10 日 = 10%
    d = make_bars([10.0] * 10)
    d["float_shares"] = 1e9
    to, days = cs.cum_turnover(d, 0, 9)
    assert days == 10
    assert abs(to - 0.10) < 1e-9


def test_cum_turnover_missing_float():
    d = make_bars([10.0] * 10)
    d["float_shares"] = np.nan
    to, days = cs.cum_turnover(d, 0, 9)
    assert to is None and days == 0


def test_classify_washout_watch():
    stage, _ = cs.classify_metrics(0.50, 30, -0.15, 0.15, 0.40)
    assert stage == "WASHOUT_WATCH"


def test_classify_boundaries_inclusive():
    # 阈值边界应放行（>= 30% / <=60d / >= -30% / >=30% 换手）
    stage, _ = cs.classify_metrics(0.30, 60, -0.30, 0.25, 0.30)
    assert stage == "WASHOUT_WATCH"


@pytest.mark.parametrize("args,expected", [
    ((0.20, 30, -0.15, 0.15, 0.40), "PRIOR_LEG_MISSING"),
    ((None, 30, -0.15, 0.15, 0.40), "PRIOR_LEG_MISSING"),
    ((0.50, 5, -0.15, 0.15, 0.40), "JUST_HIGH"),
    ((0.50, 30, -0.40, 0.15, 0.40), "BROKEN"),
    ((0.50, 90, -0.15, 0.15, 0.40), "OVER_3M"),
    ((0.50, 30, -0.15, 0.40, 0.40), "RANGE_TOO_WIDE"),
    ((0.50, 30, -0.15, 0.15, 0.10), "TURNOVER_LOW"),
    ((0.50, 30, -0.15, 0.15, None), "FLOAT_UNAVAILABLE"),
])
def test_classify_states(args, expected):
    stage, reasons = cs.classify_metrics(*args)
    assert stage == expected
    assert reasons


def test_classify_priority_broken_before_over_3m():
    # 破位优先于"超 3 个月"：深跌不是横盘洗盘
    stage, _ = cs.classify_metrics(0.50, 120, -0.45, 0.15, 0.40)
    assert stage == "BROKEN"


def test_is_fund():
    assert cs._is_fund("588000")
    assert cs._is_fund("510500")
    assert cs._is_fund("159915")
    assert cs._is_fund("161725")
    assert not cs._is_fund("600276")
    assert not cs._is_fund("300750")
    assert not cs._is_fund("110081")  # 可转债不是基金
