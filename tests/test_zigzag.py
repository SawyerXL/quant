"""ZigZag 实现测试(2026-10-19 R2 第一阶段)。

锁死: 确认滞后语义(confirm_idx>pivot_idx)、未确认末端不输出、阈值触发、
单调序列只确认起点。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zigzag import zigzag_pivots


def test_monotonic_up_confirms_initial_low_only():
    prices = [100 + i for i in range(0, 30)]  # 100→129 单调涨
    piv = zigzag_pivots(prices, 0.05)
    assert len(piv) == 1
    assert piv[0].kind == "low" and piv[0].pivot_idx == 0
    assert piv[0].confirm_idx > 0          # 确认发生在上涨超阈值那根
    assert piv[0].lag > 0


def test_v_shape_confirms_high_then_low():
    # 100 → 80(跌20%) → 100(涨25%)
    prices = [100, 98, 95, 92, 88, 84, 80, 82, 86, 90, 94, 98, 100]
    piv = zigzag_pivots(prices, 0.05)
    kinds = [x.kind for x in piv]
    assert "high" in kinds and "low" in kinds
    hi = next(x for x in piv if x.kind == "high")
    lo = next(x for x in piv if x.kind == "low")
    assert hi.pivot_idx == 0                 # 起点=局部高点
    assert lo.pivot_idx == 6                 # 80 处的低点
    assert lo.confirm_idx > lo.pivot_idx     # 确认晚于极值
    assert abs(lo.price - 80) < 1e-9


def test_unconfirmed_tail_not_emitted():
    # 一路下跌后小幅反弹未达阈值 → 无已确认 pivot 的低点
    prices = [100, 96, 92, 88, 84, 80, 81, 82]
    piv = zigzag_pivots(prices, 0.10)
    # 起点 high 在跌幅>10%时确认; 80 处低点因反弹<10%未确认
    assert all(x.kind == "high" for x in piv)
    assert not any(x.pivot_idx == 5 for x in piv)


def test_threshold_boundary():
    # 恰好 5% 反转为确认边界(>= 触发)
    prices = [100, 105, 99.75]   # 105→99.75 = -5%
    piv = zigzag_pivots(prices, 0.05)
    assert any(x.kind == "high" and abs(x.price - 105) < 1e-9 for x in piv)
