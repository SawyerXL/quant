"""核心因子和风控的单元测试。运行：pytest tests/"""
import pytest
import pandas as pd
import numpy as np
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from factors.value import bp, ep, compute_value_factors
from factors.momentum import reversal_20d, momentum_240d_minus_20d
from factors.utils import zscore, industry_zscore, composite_score


# ------------------------------------------------------------------
# 因子测试
# ------------------------------------------------------------------
class TestValueFactors:

    def test_bp_basic(self):
        price = pd.Series([10.0, 20.0, 5.0], index=["A", "B", "C"])
        bvps  = pd.Series([15.0, 10.0, 8.0], index=["A", "B", "C"])
        result = bp(price, bvps)
        assert result["A"] == pytest.approx(1.5)
        assert result["B"] == pytest.approx(0.5)

    def test_bp_zero_price(self):
        price = pd.Series([0.0, 10.0], index=["A", "B"])
        bvps  = pd.Series([5.0, 5.0],  index=["A", "B"])
        result = bp(price, bvps)
        assert pd.isna(result["A"])    # 零价格应返回 NaN，不崩溃

    def test_ep_negative_earnings(self):
        price = pd.Series([10.0], index=["A"])
        eps   = pd.Series([-1.0], index=["A"])
        result = ep(price, eps)
        assert result["A"] < 0         # 亏损公司 EP 为负，不过滤，让后续标准化处理


class TestMomentumFactors:

    def _make_close(self, n_days=300, n_stocks=5):
        dates  = pd.date_range("2022-01-01", periods=n_days, freq="B")
        prices = pd.DataFrame(
            np.cumprod(1 + np.random.randn(n_days, n_stocks) * 0.01, axis=0) * 10,
            index=dates,
            columns=[f"S{i}" for i in range(n_stocks)],
        )
        return prices

    def test_reversal_shape(self):
        close  = self._make_close()
        result = reversal_20d(close)
        assert len(result) == 5
        assert result.name == "reversal_20d"

    def test_momentum_needs_240_days(self):
        close  = self._make_close(n_days=100)   # 不够240天
        result = momentum_240d_minus_20d(close)
        assert result.isna().all()              # 数据不足时全为 NaN


class TestFactorUtils:

    def test_zscore_standardized(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        z = zscore(s)
        assert z.mean() == pytest.approx(0, abs=1e-10)
        assert z.std()  == pytest.approx(1, rel=0.01)

    def test_zscore_clip(self):
        s = pd.Series([0.0, 0.0, 0.0, 0.0, 100.0])
        z = zscore(s, clip=3.0)
        assert z.max() <= 3.0

    def test_industry_zscore_within_group(self):
        factor   = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0], index=list("ABCDE"))
        industry = pd.Series(["X", "X", "X", "Y",  "Y"],  index=list("ABCDE"))
        z = industry_zscore(factor, industry)
        # X 组内均值应为0
        x_vals = z[["A", "B", "C"]]
        assert x_vals.mean() == pytest.approx(0, abs=1e-10)

    def test_composite_score_weights(self):
        f1 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        f2 = pd.Series([3.0, 2.0, 1.0], index=["A", "B", "C"])
        # 等权合成，结果应全为2.0
        score = composite_score({"f1": f1, "f2": f2}, {"f1": 1, "f2": 1})
        assert score.values == pytest.approx([2.0, 2.0, 2.0])


# ------------------------------------------------------------------
# 风控测试
# ------------------------------------------------------------------
class TestRiskGateway:

    def _make_state(self, cash=500_000, total=1_000_000, positions=None):
        from execution.risk import RiskGateway
        state = {
            "total_assets": total,
            "cash":         cash,
            "positions":    positions or {},
            "nav_high":     total,
            "current_nav":  total,
            "strategy_positions": {"track_a": positions or {}},
        }
        return RiskGateway(state)

    def test_order_amount_limit(self):
        gw = self._make_state()
        ok, reason = gw.check("track_a", "000001", "buy", 10000, 10.0)  # 10万 > 5万上限
        assert not ok
        assert "单笔金额" in reason

    def test_normal_order_passes(self):
        gw = self._make_state()
        ok, reason = gw.check("track_a", "000001", "buy", 1000, 10.0)   # 1万，通过
        assert ok

    def test_drawdown_circuit_breaker(self):
        from execution.risk import RiskGateway
        state = {
            "total_assets": 750_000,
            "cash":         750_000,
            "positions":    {},
            "nav_high":     1_000_000,    # 历史最高100万
            "current_nav":  750_000,      # 当前75万，回撤25%
            "strategy_positions": {"track_a": {}},
        }
        gw  = RiskGateway(state)
        ok, reason = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert not ok
        assert "熔断" in reason
