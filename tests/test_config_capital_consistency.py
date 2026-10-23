"""资金常量一致性（2026-10-23）：实盘链各脚本资金常量互等，
防止"信号按40万下、NAV按100万算"的部分切换。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import check_config_alignment as c

SIM = {"GROUP_CAPITAL": 500_000, "GROUPS": {"g0": 1, "g1": 2},
       "CB_CAPITAL": 600_000, "NOTIONAL": 1_000_000,
       "NOTIONAL_CB": 600_000, "STOCK_CAPITAL": 1_000_000}


def test_aligned_sim(monkeypatch):
    monkeypatch.setattr(c, "_const", lambda p, n: SIM.get(n))
    assert c.capital_section() is True


def test_partial_switch_detected(monkeypatch):
    vals = dict(SIM, GROUP_CAPITAL=200_000)  # 信号切了、NAV/现金未切
    monkeypatch.setattr(c, "_const", lambda p, n: vals.get(n))
    assert c.capital_section() is False


def test_live_plan_enforces(monkeypatch):
    monkeypatch.setattr(c, "_const", lambda p, n: SIM.get(n))
    monkeypatch.setattr(c, "CAPITAL_PLAN", "live100w")
    assert c.capital_section() is False  # 仿真口径未切到 40万
