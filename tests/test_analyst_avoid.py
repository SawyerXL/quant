"""分析师调低回避名单的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.analyst_avoid import blacklist, env_days, get_blacklist


def _cal():
    return [d.strftime("%Y-%m-%d") for d in
            pd.date_range("2024-01-01", periods=30, freq="B")]


def test_blacklist_window_and_strict_prior(tmp_path):
    p = tmp_path / "events.parquet"
    cal = _cal()
    target = cal[25]
    pd.DataFrame({
        "date": [cal[10], cal[4], target],
        "code": ["000001", "000002", "000003"],
    }).to_parquet(p, index=False)
    got = blacklist(target, cal, 20, p)
    assert "000001" in got
    assert "000002" not in got
    assert "000003" not in got


def test_default_env_off(monkeypatch, tmp_path):
    monkeypatch.delenv("ANALYST_AVOID_DAYS", raising=False)
    assert env_days() == 0
    assert get_blacklist("2024-02-05", _cal(), tmp_path / "missing.parquet") == set()
