"""CB 评级下限 A/B 的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.cb_rating_floor_ab import select_aa_floor


def _snap():
    codes = [f"110{i:03d}" for i in range(26)]
    return pd.DataFrame({
        "code": codes,
        "rating": ["AA-"] * 25 + ["A+"],
        "size": [1.0] * 26,
        "price": [100.0 + i for i in range(26)],
        "premium": [10.0] * 26,
        "dblow": [100.0 + i for i in range(26)],
    })


def test_aa_floor_excludes_lower_rating_and_requires_25():
    snap = _snap()
    got = select_aa_floor(snap, set(snap["code"]), {})
    assert len(got) == 25
    assert "110025" not in got  # A+ 被评级下限剔除
    assert select_aa_floor(snap.iloc[:24], set(snap["code"]), {}) == []


def test_aa_floor_keeps_aa_and_above():
    snap = _snap().iloc[:25].copy()
    snap.loc[0, "rating"] = "AAA"
    snap.loc[1, "rating"] = "AA+"
    got = select_aa_floor(snap, set(snap["code"]), {})
    assert len(got) == 25
