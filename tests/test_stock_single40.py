"""股票腿单组 40 万 A/B 的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.stock_single40_ab import combo_metrics


def test_combo_metrics_reports_par_and_zero():
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    stock = pd.Series([1.0, 1.01, 1.02, 1.015, 1.03], index=idx)
    cb = pd.DataFrame({
        "nav_snapdelay_par": [1.0, 1.01, 1.02, 1.02, 1.03],
        "nav_snapdelay_zero": [1.0, 0.99, 1.0, 1.0, 1.01],
    }, index=idx)
    a = combo_metrics(stock, cb, "nav_snapdelay_par")
    b = combo_metrics(stock, cb, "nav_snapdelay_zero")
    assert a["n"] == b["n"] == 4
    assert a["metrics"]["n"] > 0
    assert "ann_pct" in a["metrics"]
