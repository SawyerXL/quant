"""CB 同宇宙基准的纯函数测试（不联网/不读库）。"""
import pandas as pd
import pytest

from scripts.cb_same_universe_benchmark import (
    compare, metrics, select_eligible_all, select_snapshot_all, select_top25,
)


def _snap(n=30):
    codes = [f"110{i:03d}" for i in range(n)]
    return pd.DataFrame({
        "code": codes,
        "rating": ["AA"] * n,
        "size": [1.0] * n,
        "price": [100.0 + i for i in range(n)],
        "premium": [10.0] * n,
        "dblow": [100.0 + i for i in range(n)],
    })


def test_top25_requires_full_basket():
    snap = _snap(30)
    avails = set(snap["code"])
    assert len(select_top25(snap, avails, {})) == 25
    assert select_top25(_snap(10), set(_snap(10)["code"]), {}) == []


def test_eligible_all_applies_shared_filter():
    snap = pd.DataFrame({
        "code": ["110001", "404001", "110003", "110004"],
        "rating": ["AA", "AA", "BBB", "AA"],
        "size": [1.0, 1.0, 1.0, 0.1],
        "price": [100.0, 100.0, 100.0, 100.0],
        "premium": [10.0, 10.0, 10.0, 10.0],
        "dblow": [100.0, 100.0, 100.0, 100.0],
    })
    got = select_eligible_all(snap, set(snap["code"]), {})
    assert got == ["110001"]
    assert select_snapshot_all(snap, set(snap["code"]), {}) == list(snap["code"])


def test_metrics_and_compare():
    idx = pd.date_range("2024-01-02", periods=4, freq="B")
    nav = pd.Series([1.0, 1.1, 1.2, 1.25], index=idx)
    m = metrics(nav, "2024-01-01", "2024-12-31")
    assert m["n"] == 4
    assert m["cum_pct"] == pytest.approx(25.0)
    assert m["maxdd_pct"] == pytest.approx(0.0)
    c = compare(nav, nav)
    assert c["relative_pp"] == pytest.approx(0.0)
    assert c["beta"] == pytest.approx(1.0)
    assert c["alpha_ann_pct"] == pytest.approx(0.0)
