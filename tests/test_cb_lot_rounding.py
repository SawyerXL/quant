"""_round_cb_lot 单元测试: CB买入张数取整到10整数倍(半进位)。
2026-09-22 废单调查: 47/57/72/74张补仓全废=非整手买入; 09-01 奇数卖出
成交=买卖不对称(卖出零头一次性合法)。取整保证买入差量幂等收敛。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.trader import _round_cb_lot


def test_round_up_half():
    assert _round_cb_lot(47) == 50
    assert _round_cb_lot(57) == 60
    assert _round_cb_lot(5) == 10


def test_round_down():
    assert _round_cb_lot(72) == 70
    assert _round_cb_lot(74) == 70
    assert _round_cb_lot(4) == 0
    assert _round_cb_lot(0) == 0


def test_exact_multiples_unchanged():
    assert _round_cb_lot(210) == 210
    assert _round_cb_lot(10) == 10


def test_convergence():
    # 09-21 实盘情形: 持仓163 目标210 → 差47 → 取整50 → 213;
    # 次日差 -3 ≤ 0 不再下单(幂等收敛, 漂移≤5张)
    cur, tgt = 163, 210
    buy1 = _round_cb_lot(tgt - cur)
    cur += buy1
    assert buy1 == 50
    assert _round_cb_lot(tgt - cur) <= 0 or (tgt - cur) <= 5
