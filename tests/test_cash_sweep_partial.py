"""cash_sweep 部分卖出补丁的测试（补丁未应用时 skip）。"""
import pytest

from scripts import cash_sweep as cs

if not hasattr(cs, "_sell_qty"):
    pytest.skip("cash_sweep partial-sell patch not applied", allow_module_level=True)


def test_capital_cut_sells_only_excess_and_caps_order():
    # 400k 目标 280k，持有 695,973（6,100 份 @114），单笔≤10万 → 800 份
    assert cs._sell_qty(6100, 695973, 280000, 114, 0.3) == 800


def test_high_tier_sells_all():
    assert cs._sell_qty(6100, 695973, 0, 114, 0.6) == 6100


def test_no_excess_does_nothing():
    assert cs._sell_qty(6100, 695973, 700000, 114, 0.3) == 0


def test_bad_inputs_return_zero():
    assert cs._sell_qty(0, 0, 0, 0, 0.3) == 0
    assert cs._sell_qty(6100, 695973, 0, 0, 0.3) == 0
