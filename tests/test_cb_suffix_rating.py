"""后缀评级映射修复的单元测试(2026-10-14, 十三轮裁定: 剥后缀映射双向生效)。"""
import pandas as pd
import pytest

from data.cb_select import filter_universe


def make_snap(rows):
    base = {
        "code": [r[0] for r in rows],
        "rating": [r[1] for r in rows],
        "size": [10.0] * len(rows),
        "price": [110.0] * len(rows),
        "premium": [10.0] * len(rows),
        "dblow": [110.0] * len(rows),
    }
    return pd.DataFrame(base)


def test_suffix_ratings_pass_filter():
    """AAsti/AA+sti/A+sti 应通过 ≥A- 过滤(修复前被 fillna(99) 误剔)。"""
    snap = make_snap([
        ("110001", "AAsti"), ("110002", "AA+sti"), ("110003", "A+sti"),
        ("110004", "AAAsti"), ("110005", "AA-sti"),
    ])
    out = filter_universe(snap)
    assert set(out["code"]) == {"110001", "110002", "110003", "110004", "110005"}


def test_suffix_strip_does_not_leak_below_threshold():
    """BBB+sti 剥后缀后=BBB+ 仍低于 A-, 不得因剥后缀混入。"""
    snap = make_snap([("110001", "AAsti"), ("110002", "BBB+sti"),
                      ("110003", "C")])
    out = filter_universe(snap)
    assert set(out["code"]) == {"110001"}


def test_plain_ratings_unaffected():
    """无后缀评级行为不变(回归)。"""
    snap = make_snap([("110001", "AA"), ("110002", "A-"),
                      ("110003", "BBB-"), ("110004", None)])
    out = filter_universe(snap)
    assert set(out["code"]) == {"110001", "110002"}


def test_suffix_fix_known_case_127038():
    """已知实锤案例: 127038 国微转债 AA+sti 修复前被误剔(rank16)。"""
    snap = make_snap([("127038", "AA+sti")])
    out = filter_universe(snap)
    assert "127038" in set(out["code"])
