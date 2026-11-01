"""C0006 纯函数测试（不联网/不读库）。"""
import pandas as pd
import pytest

from scripts.c0006_overnight_backtest import period_stats, summarize_series, verdict_for


def test_summarize_series_day_cluster():
    x = pd.Series([0.01, -0.01, 0.02],
                  index=["2024-01-02", "2024-01-03", "2024-01-04"])
    s = summarize_series(x)
    assert s["n_days"] == 3
    assert s["mean_bp"] == pytest.approx(66.6667, abs=1e-3)
    assert s["median_bp"] == pytest.approx(100.0)
    assert s["win_rate_pct"] == pytest.approx(66.67)
    assert s["mde_bp"] > 0
    assert s["cum_pct"] == pytest.approx(1.9898, abs=1e-3)


def test_period_stats_filters_and_empty():
    x = pd.Series([0.01, 0.02, 0.03],
                  index=["2023-12-29", "2024-01-02", "2024-01-03"])
    s = period_stats(x, "2024-01-01", "2024-12-31")
    assert s["n_days"] == 2
    assert s["mean_bp"] == pytest.approx(250.0)
    assert period_stats(x, "2025-01-01", "2025-12-31") == {"n_days": 0}


def _out(oos_mean, oos_mde, oos_n=300, is_mean=1.0):
    return {
        "periods": {
            "OOS": {"half_diff": {"n_days": oos_n, "mean_bp": oos_mean, "mde_bp": oos_mde}},
            "IS": {"half_diff": {"mean_bp": is_mean}},
        }
    }


def test_verdict_thresholds():
    assert verdict_for(_out(6.0, 5.0)) == "PASS"
    assert verdict_for(_out(4.0, 5.0)) == "FAIL"
    assert verdict_for(_out(6.0, 5.0, is_mean=-1.0)) == "FAIL"
    assert verdict_for(_out(6.0, 5.0, oos_n=100)) == "UNDERPOWERED"
    assert verdict_for(_out(6.0, None)) == "UNDERPOWERED"
