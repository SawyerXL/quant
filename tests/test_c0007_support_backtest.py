"""C0007 机械代理的纯函数测试（不联网/不读库）。"""
import pandas as pd
import pytest

from scripts.c0007_support_backtest import find_setups, first_touch, summarize


def _bars():
    n = 35
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    open_ = [10.0] * n
    high = [10.5] * n
    low = [10.0] * n
    close = [10.0] * n
    low[5] = 8.0          # pivot low A
    low[15] = 11.0
    high[15] = 12.0       # pivot high B
    low[23] = 9.2         # first touch of S=9.333
    close[23] = 9.5
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": close,
    })


def test_find_setups_and_touch():
    df = _bars()
    setups = find_setups(df, k=5)
    assert len(setups) >= 1
    st = setups[0]
    assert st["a"] == pytest.approx(8.0)
    assert st["b"] == pytest.approx(12.0)
    assert st["s"] == pytest.approx(8.0 + (12.0 - 8.0) / 3)
    hit = first_touch(df, st)
    assert hit is not None
    t, entry = hit
    assert t == 23
    assert entry == pytest.approx(st["s"])


def test_summarize_reports_mde_and_days():
    ev = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04"],
        "ex10": [0.01, 0.02, -0.01, 0.005],
    })
    s = summarize(ev, "2024-01-01", "2024-01-31", "ex10")
    assert s["n_events"] == 4
    assert s["n_days"] == 3
    assert s["mean_event_pct"] == pytest.approx(0.625)
    assert s["mean_day_pct"] == pytest.approx(0.3333, abs=1e-3)
    assert s["mde_pct"] is not None
