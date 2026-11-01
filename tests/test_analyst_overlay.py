"""分析师调低 overlay 的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.analyst_overlay_ab import build_exclude_map


def _cal():
    return [d.strftime("%Y-%m-%d") for d in
            pd.date_range("2024-01-01", periods=30, freq="B")]


def test_exclude_recent_downgrade_only():
    cal = _cal()
    target = cal[25]
    neg = pd.DataFrame({
        "code": ["000001", "000002", "000003"],
        "date": [cal[10], cal[4], target],  # 15日前/21日前/当日
    })
    ex = build_exclude_map(cal, [target], neg)[target]
    assert "000001" in ex
    assert "000002" not in ex  # 超出 20 日
    assert "000003" not in ex  # 当日不排除
