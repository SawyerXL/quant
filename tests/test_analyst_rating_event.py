"""分析师评级事件研究的纯函数测试（不联网/不读库）。"""
import pandas as pd
import pytest

from scripts.analyst_rating_event import classify_events, summarize


def test_classify_positive_priority_and_negative():
    d = pd.DataFrame({
        "code": ["000001", "000001", "000002", "000003"],
        "date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04"],
        "评级变化": ["调高", "调低", "调低", "维持"],
        "是否首次评级": ["不是首次评级", "不是首次评级", "不是首次评级",
                     "是首次评级"],
    })
    ev = classify_events(d)
    assert len(ev) == 3
    assert set(ev.loc[ev["code"] == "000001", "kind"]) == {"positive"}
    assert set(ev.loc[ev["code"] == "000002", "kind"]) == {"negative"}
    assert set(ev.loc[ev["code"] == "000003", "kind"]) == {"positive"}


def test_summarize_positive_day_cluster():
    ev = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"],
        "kind": ["positive"] * 4,
        "ex20": [0.01, 0.02, -0.01, 0.00],
    })
    s = summarize(ev, "2024-01-01", "2024-12-31", "ex20", "positive")
    assert s["n_events"] == 4
    assert s["n_days"] == 2
    assert s["mean_day_pct"] == pytest.approx(0.5)
    assert s["mde_pct"] is not None
