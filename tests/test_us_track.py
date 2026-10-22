"""US 独立 track 最小链路测试（2026-10-22）。

覆盖：目标股数/拆单上限/溢价闸门/单票 100%/sleeve 无 15% 熔断。
运行：.venv/bin/python -m pytest tests/test_us_track.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import us_track
from us_track import (split_orders, gate_pass, _target_shares,
                      USRiskGateway, VEHICLE, CAP)


def _state(positions=None, cash=CAP, nav_high=CAP, nav=CAP):
    return {
        "total_assets": CAP, "cash": cash,
        "positions": positions or {},
        "nav_high": nav_high, "current_nav": nav,
        "strategy_positions": {"track_us": positions or {}},
        "industry_map": {},
        "limit_prices": lambda code: {"up": 1e9, "down": 0.0},
    }


class TestSizing:
    def test_target_shares_lot100(self):
        sh = _target_shares(199_940, 1.321)
        assert sh % 100 == 0 and sh * 1.321 <= 199_940

    def test_split_orders_cap_and_sum(self):
        px = 1.321
        orders = split_orders(151_300, px)
        assert sum(orders) == 151_300
        assert all(q * px <= 100_000 for q in orders)
        assert all(q % 100 == 0 for q in orders)

    def test_split_rejects_unaffordable(self):
        try:
            split_orders(100, 2000.0)   # 一手2000股? 100股×2000=20万>10万
        except ValueError:
            return
        # max_qty = floor(100000/2000/100)*100 = 0 → 应抛
        raise AssertionError("应抛 ValueError")


class TestPremiumGate:
    def test_below_and_at_threshold_pass(self):
        assert gate_pass(4.99)
        assert gate_pass(5.00)

    def test_above_threshold_fails(self):
        assert not gate_pass(5.01)

    def test_missing_premium_fails(self):
        assert not gate_pass(None)


class TestUSRisk:
    def test_single_order_under_100k_passes(self):
        gw = USRiskGateway(_state())
        ok, msg = gw.check("track_us", VEHICLE, "buy", 75_700, 1.321)
        assert ok, msg

    def test_single_order_over_100k_blocked(self):
        gw = USRiskGateway(_state())
        ok, msg = gw.check("track_us", VEHICLE, "buy", 151_500, 1.321)
        assert not ok and ("上限" in msg or "超过" in msg)

    def test_full_sleeve_single_etf_allowed(self):
        # 已持 10 万，再买 ~10 万 → 99.9995% ≤ 100%，通过
        gw = USRiskGateway(_state(positions={VEHICLE: 100_000}))
        ok, msg = gw.check("track_us", VEHICLE, "buy", 75_700, 1.321)
        assert ok, msg

    def test_sleeve_drawdown_does_not_freeze(self):
        # 70% sleeve 回撤：track_a 会熔断，track_us 不冻结
        gw = USRiskGateway(_state(nav_high=CAP, nav=CAP * 0.3))
        ok, msg = gw.check("track_us", VEHICLE, "buy", 100, 1.321)
        assert ok, msg


class TestPaperFlow:
    """端到端纸面成交/mark（临时路径，不碰真实状态）。"""

    def _setup(self, tmp_path, monkeypatch, as_of="2026-10-22 14:45:00"):
        monkeypatch.setattr(us_track, "STATE", tmp_path / "state.json")
        monkeypatch.setattr(us_track, "SIGNAL", tmp_path / "signal.json")
        monkeypatch.setattr(us_track, "DAILY", tmp_path / "daily.csv")
        monkeypatch.setattr(us_track, "_trading_day", lambda d: True)
        us_track._save_state(us_track._state_init())
        sig = {"as_of": as_of, "signal_date": "2026-10-22", "side": "buy",
               "price": 1.30, "orders": [1000], "risk_pass": True,
               "premium_pct": 2.3, "gate_pass": True}
        (tmp_path / "signal.json").write_text(json.dumps(sig), encoding="utf-8")

    def test_fill_then_guard_then_mark(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        s = us_track.paper_fill()
        assert s["shares"] == 1000
        assert s["cash"] == pytest.approx(CAP - 1000 * 1.30 * (1 + us_track.FEE),
                                          abs=0.02)
        # 同一天重复 fill 必须跳过（不重复建仓）
        s2 = us_track.paper_fill()
        assert s2["shares"] == 1000
        monkeypatch.setattr(us_track, "tencent_quote",
                            lambda code: {"price": 1.31, "iopv": 1.29,
                                          "premium_pct": 1.55})
        s3 = us_track.mark()
        assert s3["shares"] == 1000
        assert s3["nav"] == pytest.approx(s3["cash"] + 1000 * 1.31, abs=0.02)
        assert Path(us_track.DAILY).exists()

    def test_buy_before_1440_is_rejected(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, as_of="2026-10-22 10:00:00")
        with pytest.raises(RuntimeError):
            us_track.paper_fill()
