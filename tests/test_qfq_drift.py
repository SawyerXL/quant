"""qfq 漂移分类测试（2026-10-22）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from qfq_drift_check import classify


def test_ok_within_tolerance():
    assert classify([0.0001, -0.0002, 0.0003], 0.003) == "ok"


def test_anchor_shift_uniform_same_sign():
    # 002250 实证：四日同向 −0.66%~−0.76%
    assert classify([-0.00758, -0.00758, -0.00662, -0.00729], 0.003) == "anchor_shift"


def test_mismatch_non_uniform():
    assert classify([-0.007, 0.001, -0.01], 0.003) == "mismatch"


def test_no_data():
    assert classify([], 0.003) == "no_data"
    assert classify([None, None], 0.003) == "no_data"
