"""C0005 描述性试点的纯函数测试（不联网）。"""
import pandas as pd
import pytest

from scripts.c005_execution_pilot import drift_frame, summarize


def _bars():
    return pd.DataFrame({
        "day": pd.to_datetime([
            "2026-10-29 14:30", "2026-10-29 15:00",
            "2026-10-30 14:30", "2026-10-30 15:00",
        ]),
        "close": [100.0, 101.0, 100.0, 99.0],
        "high": [100, 101, 100, 99],
        "low": [99, 100, 99, 98],
    })


def test_drift_frame_uses_1430_and_1500():
    d = drift_frame(_bars(), "110092")
    assert len(d) == 2
    assert d["ret"].tolist() == pytest.approx([0.01, -0.01])
    assert d["p1430"].tolist() == [100.0, 100.0]


def test_summarize_reports_days_and_mean():
    d = drift_frame(_bars(), "110092")
    s = summarize(d)
    assert s["n_obs"] == 2
    assert s["n_days"] == 2
    assert s["mean_bp"] == pytest.approx(0.0)
    assert s["win_rate_pct"] == pytest.approx(50.0)
    assert s["by_code_mean_bp"]["110092"] == pytest.approx(0.0)
