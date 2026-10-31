"""live-vs-backtest 跟踪的核心口径测试（合成数据，不依赖 QMT/网络）。"""
import pandas as pd
import pytest

from scripts import live_performance_track as mod


def _dates(n):
    return pd.date_range("2026-01-05", periods=n, freq="B")


def test_normalize_and_max_drawdown():
    s = pd.Series([2.0, 3.0, 1.5], index=_dates(3))
    assert mod.normalize(s).iloc[0] == 1.0
    assert mod.max_drawdown(s) == pytest.approx(-0.5)


def test_tracking_aligns_common_dates_and_normalizes_at_common_start():
    idx = _dates(4)
    live = pd.Series([1.00, 1.10, 1.05, 1.20], index=idx)
    bt = pd.Series([9.72, 9.54, 10.35], index=idx[1:])
    tr = mod.tracking(live, bt)
    assert tr["n"] == 3
    assert tr["start"] == str(idx[1].date())
    # 共同起点归一：live 1.20/1.10-1=9.09%；bt 10.35/9.72-1=6.48%
    assert tr["cum_live_pct"] == pytest.approx(9.091, abs=1e-3)
    assert tr["cum_bt_pct"] == pytest.approx(6.481, abs=1e-3)
    assert tr["excess_pp"] == pytest.approx(2.610, abs=1e-3)


def test_tracking_reports_insufficient_overlap():
    live = pd.Series([1.0, 1.1], index=_dates(2))
    bt = pd.Series([1.0, 1.1], index=pd.date_range("2027-01-04", periods=2))
    out = mod.tracking(live, bt)
    assert out["n"] == 0
    assert "error" in out


def test_metrics_short_sample_has_no_annualized():
    s = pd.Series([1.0, 1.01, 1.02], index=_dates(3))
    m = mod.metrics(s)
    assert m["annualized_meaningful"] is False
    assert m["ann_pct"] is None
    assert m["cum_pct"] == pytest.approx(2.0)


def test_tracking_beta_alpha_on_scaled_series():
    idx = _dates(6)
    bt_ret = pd.Series([0.01, -0.02, 0.015, 0.005, -0.01], index=idx[1:])
    live_ret = 2 * bt_ret + 0.001
    bt = (1 + bt_ret).cumprod()
    live = (1 + live_ret).cumprod()
    bt = pd.concat([pd.Series([1.0], index=idx[:1]), bt])
    live = pd.concat([pd.Series([1.0], index=idx[:1]), live])
    tr = mod.tracking(live, bt)
    assert tr["beta"] == pytest.approx(2.0, abs=1e-6)
    assert tr["alpha_ann_pct"] == pytest.approx(25.2, abs=1e-6)
    assert tr["corr"] == pytest.approx(1.0, abs=1e-9)


def test_exposure_segments_attributes_only_in_segment_days():
    idx = _dates(5)
    live = pd.DataFrame({
        "n_pos_a": [20, 20, 1, 1, 1],
        "nav_a": [1.00, 1.01, 1.02, 1.03, 1.04],
        "nav_cb": [1.00, 0.99, 0.98, 0.97, 0.96],
        "nav_combined": [1.000, 1.005, 1.010, 1.015, 1.020],
    }, index=idx)
    seg = mod.exposure_segments(live)
    assert seg["stock_active"]["n_days"] == 2
    assert seg["stock_active"]["nav_a_pct"] == pytest.approx(1.0)
    assert seg["stock_active"]["nav_cb_pct"] == pytest.approx(-1.0)
    assert seg["stock_active"]["nav_combined_pct"] == pytest.approx(0.5)
    assert seg["cash_only"]["n_days"] == 3
    assert seg["cash_only"]["nav_a_pct"] == pytest.approx(1.9608, abs=1e-3)
    assert seg["cash_only"]["nav_cb_pct"] == pytest.approx(-2.0408, abs=1e-3)
    assert seg["cash_only"]["nav_combined_pct"] == pytest.approx(0.9901, abs=1e-3)


def test_cb_vs_index_relative_uses_common_window(tmp_path, monkeypatch):
    idx = pd.DataFrame({
        "date": _dates(3),
        "close": [100.0, 90.0, 80.0],
    })
    p = tmp_path / "cb_index.parquet"
    idx.to_parquet(p)
    monkeypatch.setattr(mod, "CB_INDEX", p)
    live = pd.DataFrame(
        {"nav_cb": [1.0, 0.98, 0.96]},
        index=_dates(3),
    )
    out = mod.cb_vs_index(live)
    assert out["n"] == 3
    assert out["cb_cum_pct"] == pytest.approx(-4.0)
    assert out["index_cum_pct"] == pytest.approx(-20.0)
    assert out["relative_pp"] == pytest.approx(16.0)
