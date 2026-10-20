"""apply_filters 的 exclude 钩子单测(2026-10-20, #21 overlay)。

默认 exclude=None 行为不变; 非持仓被排除并由下一名补位; 已持仓豁免(不强制卖)。
"""
from dataclasses import replace

import numpy as np
import pandas as pd

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from backtest_config import DEFAULT_CONFIG
from backtest_engine import apply_filters


def _panel(n=70, codes=("A", "B", "C", "D")):
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    data = {c: np.linspace(10, 20, n) * (1 + 0.001 * i)
            for i, c in enumerate(codes)}
    return pd.DataFrame(data, index=idx)


def test_default_unchanged():
    p = _panel()
    cfg = replace(DEFAULT_CONFIG, pool_size=3)
    out = apply_filters(["A", "B", "C", "D"], p, 69, cfg)
    assert out == ["A", "B", "C"]


def test_exclude_nonheld_fills_next():
    p = _panel()
    cfg = replace(DEFAULT_CONFIG, pool_size=3)
    out = apply_filters(["A", "B", "C", "D"], p, 69, cfg, exclude={"B"})
    assert out == ["A", "C", "D"]
    assert "B" not in out


def test_held_exempt_not_forced_out():
    p = _panel()
    cfg = replace(DEFAULT_CONFIG, pool_size=3)
    out = apply_filters(["A", "B", "C", "D"], p, 69, cfg,
                        exclude={"B"}, held={"B"})
    assert out == ["A", "B", "C"]


def test_early_path_exclude():
    p = _panel()
    cfg = replace(DEFAULT_CONFIG, pool_size=3)
    out = apply_filters(["A", "B", "C", "D"], p, 10, cfg, exclude={"A"})
    assert out == ["B", "C", "D"]
