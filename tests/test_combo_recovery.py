"""40/60 回收敏感性脚本的纯函数测试（不联网/不读库）。"""
import pandas as pd

from scripts.combo_recovery_sensitivity import run


def test_run_reports_metrics():
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    stock = pd.Series([1.0, 1.01, 1.02, 1.015, 1.03], index=idx)
    cb = pd.Series([0.0, 0.01, -0.01, 0.02, 0.0], index=idx)
    nav, meta = run(stock, cb)
    assert len(nav) == len(stock) - 1
    assert meta["n"] == len(stock) - 1
    assert meta["metrics"]["n"] > 0
    assert "ann_pct" in meta["metrics"]
