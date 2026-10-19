"""撤单状态回归(2026-10-19 实锤): status 54(已撤)是终态, 不得并入持仓。

现场: 09:41 127068 首单撤(54,filled0)+修正单成(56,filled200); 14:30 链
把撤单未成交量 200 并入持仓(200实持→400), 派生价 mv/vol 腰斩 115→57.6,
挂出 56.44 卖单——被链隔离锁拦下。本测试锁死修复。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.trader import Trader
from execution.qmt_client import get_client


def _trader(monkeypatch, positions, today_orders):
    client = get_client()
    client._positions = positions
    client.get_today_orders = lambda: today_orders
    monkeypatch.setattr(Trader, "__init__",
                        lambda self: setattr(self, "client", client))
    return Trader()


def _sig():
    return {"signal_date": "2026-10-19", "track": "cb", "capital": 600000,
            "holdings": ["127068"], "shares": {"127068": 200},
            "prices": {"127068": 114.98}, "buy": [], "sell": [], "excluded": {}}


def test_cancelled_order_not_counted(monkeypatch):
    """撤单(54)+已成(56) → 持仓=200, 残差=0, 不得产生卖单。"""
    t = _trader(monkeypatch,
                {"127068.SZ": {"volume": 200, "cost_price": 114.58,
                               "market_value": 23044}},
                [{"code": "127068.SZ", "direction": "buy", "shares": 200,
                  "filled": 0, "status": 54},
                 {"code": "127068.SZ", "direction": "buy", "shares": 200,
                  "filled": 200, "status": 56}])
    plan = t.compute_plan("track_cb", _sig())
    assert plan["sells"] == [], plan["sells"]


def test_partially_filled_open_still_counted(monkeypatch):
    """部成未终态(55)的未成交量仍应并入(防重复买入)。"""
    t = _trader(monkeypatch,
                {"127068.SZ": {"volume": 100, "cost_price": 114.58,
                               "market_value": 11522}},
                [{"code": "127068.SZ", "direction": "buy", "shares": 200,
                  "filled": 100, "status": 55}])
    plan = t.compute_plan("track_cb", _sig())
    # 100 实持 + 100 未成 = 200 = 目标 → 不买不卖
    assert plan["buys"] == [] and plan["sells"] == []


def test_cancelled_sell_not_counted(monkeypatch):
    """撤掉的卖单不得把持仓扣成负数/触发错误买入。"""
    t = _trader(monkeypatch,
                {"127068.SZ": {"volume": 200, "cost_price": 114.58,
                               "market_value": 23044}},
                [{"code": "127068.SZ", "direction": "sell", "shares": 200,
                  "filled": 0, "status": 54}])
    plan = t.compute_plan("track_cb", _sig())
    assert plan["sells"] == [] and plan["buys"] == []
