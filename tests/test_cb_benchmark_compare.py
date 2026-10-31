"""CB 多基准比较的纯函数测试（不联网）。"""
import pandas as pd
import pytest

from scripts.cb_benchmark_compare import compare, total_return_proxy


def _dates(n):
    return pd.date_range("2026-07-20", periods=n, freq="B")


def test_total_return_proxy_adds_daily_coupon():
    s = pd.Series([100.0, 100.0, 100.0], index=_dates(3))
    tr = total_return_proxy(s, coupon=0.0252)
    assert tr.iloc[0] == pytest.approx(1.0)
    assert tr.iloc[1] == pytest.approx(1.0001)
    assert tr.iloc[2] == pytest.approx(1.00020001)


def test_compare_beta_alpha_on_scaled_series():
    idx = _dates(6)
    br = pd.Series([0.01, -0.02, 0.015, 0.005, -0.01], index=idx[1:])
    lr = 0.5 * br + 0.001
    bench = (1 + br).cumprod()
    live = (1 + lr).cumprod()
    bench = pd.concat([pd.Series([1.0], index=idx[:1]), bench])
    live = pd.concat([pd.Series([1.0], index=idx[:1]), live])
    out = compare(live, bench)
    assert out["n"] == 6
    assert out["beta"] == pytest.approx(0.5, abs=1e-6)
    assert out["alpha_ann_pct"] == pytest.approx(25.2, abs=1e-6)
    assert out["corr"] == pytest.approx(1.0, abs=1e-9)
