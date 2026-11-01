"""融资余额拥挤度事件的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.margin_flow_event import assign_decile, summarize


def test_assign_decile_monotonic():
    ev = pd.DataFrame({
        "date": ["2024-01-31"] * 20,
        "ratio": list(range(20)),
        "ex20": [0.01] * 20,
    })
    out = assign_decile(ev)
    assert out["dec"].min() == 0
    assert out["dec"].max() == 9
    assert int((out["dec"] == 0).sum()) == 2


def test_summarize_low_decile_day_cluster():
    ev = pd.DataFrame({
        "date": ["2024-01-31", "2024-01-31", "2024-02-29", "2024-02-29"],
        "dec": [0, 0, 0, 0],
        "ex20": [0.01, 0.02, -0.01, 0.00],
    })
    s = summarize(ev, "2024-01-01", "2024-12-31", "ex20", 0)
    assert s["n_events"] == 4
    assert s["n_days"] == 2
    assert s["mean_day_pct"] == 0.5
    assert s["mde_pct"] is not None
