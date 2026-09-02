"""
Trader 执行器行为测试（2026-09-02，执行链修复后新增——此前生产引擎零真实覆盖）。

覆盖本次修复的四条语义：
  1. 风控分母 = track名义资金(非QMT污染total_assets)
  2. nav_high sanity: 污染净值拒绝写入
  3. 持仓无shares条目 → fail-safe跳过卖出+告警(9/1事故路径)
  4. 涨跌停检查: 涨停禁买/跌停禁卖经gateway拦截
运行: .venv/bin/python -m pytest tests/test_trader.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch, tmp_path):
    """所有测试用 MockQMTClient + 临时信号文件, 不碰真实QMT。"""
    import execution.trader as trader_mod

    client = trader_mod.get_client()
    monkeypatch.setattr(trader_mod.Trader, "__init__",
                        lambda self: setattr(self, "client", client))
    monkeypatch.setattr(trader_mod, "_NAV_HIGH_FILE", tmp_path / "nav_high.json")
    yield {"client": client, "tmp": tmp_path}


def _write_signal(tmp, sig: dict) -> str:
    p = tmp / "signal_test.json"
    p.write_text(json.dumps(sig), encoding="utf-8")
    return str(p)


# ── 修复1: 风控分母按track名义资金 ────────────────────────────────

class TestGatewayCapital:

    def test_polluted_total_assets_ignored(self, _mock_env, monkeypatch):
        """QMT total_assets=4837万污染值时, 网关分母=信号capital(40万)。"""
        from execution.trader import Trader
        client = _mock_env["client"]
        monkeypatch.setattr(client, "get_account_info",
                            lambda: {"total_assets": 48_000_000, "cash": 48_000_000,
                                     "market_value": 0})
        t = Trader()
        from execution.risk import RiskGateway
        gw, _ = t._build_gateway("track_a", 400_000)
        assert gw.state["total_assets"] == 400_000
        assert gw.state["cash"] == 400_000

    def test_nav_high_pollution_rejected(self, _mock_env, monkeypatch):
        """污染净值(4837万)超出sanity带 → 不写入nav_high文件。"""
        from execution.trader import Trader
        client = _mock_env["client"]
        monkeypatch.setattr(client, "get_account_info",
                            lambda: {"total_assets": 48_000_000, "cash": 48_000_000,
                                     "market_value": 0})
        t = Trader()
        t._build_gateway("track_a", 400_000)
        from execution.trader import _NAV_HIGH_FILE
        saved = json.loads(_NAV_HIGH_FILE.read_text()) if _NAV_HIGH_FILE.exists() else {}
        assert saved.get("track_a", 0) < 1_500_000, "污染净值不得写入nav_high"


# ── 修复3: 无shares持仓fail-safe ─────────────────────────────────

class TestMissingSharesFailSafe:

    def test_holding_without_shares_not_sold(self, _mock_env):
        """持仓在holdings但无shares条目 → 跳过卖出并告警, 不误清仓。"""
        from execution.trader import Trader
        client = _mock_env["client"]
        # 实盘已有持仓(带后缀key), 信号里holdings含该票但shares无条目
        client._positions = {
            "600030.SH": {"volume": 800, "cost_price": 28.6, "market_value": 22880},
        }
        sig = {
            "signal_date": "2026-09-02", "regime": "bull",
            "holdings": ["600030", "600176"],
            "shares": {"600176": 300},
            "prices": {"600030": 28.6, "600176": 42.0},
            "sell": [], "buy": [],
        }
        t = Trader()
        res = t.execute_signal(_write_signal(_mock_env["tmp"], sig), "track_a")
        sell_codes = [s["code"] for s in res["sells"]]
        assert "600030" not in sell_codes, "无shares持仓不得被误卖"
        # 有shares的票正常差量买入
        assert any(b["code"] == "600176" for b in res["buys"])

    def test_sell_set_still_sells_without_shares(self, _mock_env):
        """显式sell清单的票(无shares条目)仍要卖出——MA10出清路径。"""
        from execution.trader import Trader
        client = _mock_env["client"]
        client._positions = {
            "300433.SZ": {"volume": 500, "cost_price": 33.9, "market_value": 16950},
        }
        sig = {
            "signal_date": "2026-09-02", "regime": "bull",
            "holdings": [], "shares": {},
            "prices": {"300433": 33.9},
            "sell": ["300433"], "buy": [],
        }
        t = Trader()
        res = t.execute_signal(_write_signal(_mock_env["tmp"], sig), "track_a")
        assert any(s["code"] == "300433" for s in res["sells"])


# ── 修复4: 涨跌停检查 ─────────────────────────────────────────────

class TestLimitPriceEnforcement:

    def _limit_gw(self, caps):
        from execution.trader import Trader
        t = Trader()
        from execution.risk import RiskGateway
        gw = RiskGateway({
            "total_assets": 400_000, "cash": 400_000, "positions": {},
            "nav_high": 1.0, "current_nav": 1.0,
            "strategy_positions": {"track_a": {}},
            "industry_map": {},
            "limit_prices": caps,
        })
        return gw

    def test_buy_at_limit_up_blocked(self):
        gw = self._limit_gw({"000001": {"up": 11.0, "down": 9.0}})
        ok, msg = gw.check("track_a", "000001", "buy", 100, 11.0)
        assert not ok and "涨停" in msg

    def test_sell_at_limit_down_blocked(self):
        gw = self._limit_gw({"000001": {"up": 11.0, "down": 9.0}})
        ok, msg = gw.check("track_a", "000001", "sell", 100, 9.0)
        assert not ok and "跌停" in msg
