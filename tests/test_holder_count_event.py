"""股东户数事件研究的纯函数测试（不联网/不读库）。"""
import pandas as pd
import pytest

from scripts.holder_count_event import assign_top_quintile, summarize


def test_assign_top_quintile_most_negative():
    ev = pd.DataFrame({
        "date": ["2024-05-01"] * 10,
        "chg": list(range(-10, 0)),  # -10 最集中
        "ex20": [0.01] * 10,
    })
    out = assign_top_quintile(ev)
    assert int(out["top"].sum()) == 2
    assert set(out.loc[out["top"] == 1, "chg"]) == {-10, -9}


def test_summarize_top_quintile_day_cluster():
    ev = pd.DataFrame({
        "date": ["2024-05-01", "2024-05-01", "2024-05-02", "2024-05-02"],
        "top": [1, 1, 1, 1],
        "ex20": [0.01, 0.02, -0.01, 0.00],
    })
    s = summarize(ev, "2024-01-01", "2024-12-31", "ex20")
    assert s["n_events"] == 4
    assert s["n_days"] == 2
    assert s["mean_day_pct"] == pytest.approx(0.5)
    assert s["mde_pct"] is not None
