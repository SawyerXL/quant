"""高质押 overlay 的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.pledge_overlay_ab import build_exclude_map


def test_pledge_uses_latest_strictly_prior_snapshot():
    cal = [d.strftime("%Y-%m-%d") for d in
           pd.date_range("2024-01-01", periods=10, freq="B")]
    snaps = ["2024-01-03", "2024-01-05"]
    by = {"2024-01-03": {"000001"}, "2024-01-05": {"000002"}}
    target = "2024-01-08"
    ex = build_exclude_map(cal, [target], snaps, by)[target]
    assert ex == {"000002"}
    before = "2024-01-03"
    ex2 = build_exclude_map(cal, [before], snaps, by)[before]
    assert ex2 == set()
