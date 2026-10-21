"""compute_plan 差量计划 + classify_tier 档位分流 测试 (2026-09-24)
计划口径必须与 _execute_bull 一致(CB取整10/清仓全卖/死区/废单跳过),
档位规则按用户裁定: 档3(总>30万/新增>8/单笔>5万/闲置部署) 档1(单笔<2万
且非新增) 档2(其余)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.trader import Trader
from execution.qmt_client import get_client


def _mk_signal(holdings, shares, prices, buy=None, sell=None, **kw):
    sig = {"signal_date": "2026-09-24", "track": "cb", "capital": 600000,
           "holdings": holdings, "shares": shares, "prices": prices,
           "buy": buy or [], "sell": sell or []}
    sig.update(kw)
    return sig


def _trader_with_positions(monkeypatch, positions):
    client = get_client()
    client._positions = positions
    monkeypatch.setattr(Trader, "__init__",
                        lambda self: setattr(self, "client", client))
    return Trader()


def test_plan_diff_rounding_and_clears(monkeypatch):
    # 09-24 实盘形态: 持仓奇数张+临期清仓+差量取整
    t = _trader_with_positions(monkeypatch, {
        "123107.SZ": {"volume": 163, "cost_price": 114.0, "market_value": 18582},
        "128134.SZ": {"volume": 190, "cost_price": 121.3, "market_value": 23047},
        "127061.SZ": {"volume": 200, "cost_price": 116.5, "market_value": 23300},
        "128142.SZ": {"volume": 136, "cost_price": 112.4, "market_value": 15286},
        "127027.SZ": {"volume": 153, "cost_price": 111.7, "market_value": 17090},
    })
    sig = _mk_signal(
        ["123107", "127061", "127040", "128142", "127027"],
        {"123107": 210, "127061": 210, "127040": 190,
         "128142": 136, "127027": 153},
        {"123107": 111.5, "127061": 108.2, "127040": 115.1,
         "128142": 112.4, "127027": 111.7})
    plan = t.compute_plan("track_cb", sig)
    sells = {e["code"]: e["shares"] for e in plan["sells"]}
    buys = {e["code"]: e["shares"] for e in plan["buys"]}
    assert sells == {"128134": 190}          # 不在holdings → 清仓全卖
    assert buys["123107"] == 50              # 47 半进位取整
    assert buys["127061"] == 10
    assert buys["127040"] == 190
    assert "128142" not in buys and "127027" not in buys   # 目标=持仓, 差量0
    # 差量对照字段
    b = next(e for e in plan["buys"] if e["code"] == "123107")
    assert b["held"] == 163 and b["target"] == 210


def test_plan_sell_deadzone(monkeypatch):
    # CB 减仓差量<10张不交易(死区), 清仓不受限
    t = _trader_with_positions(monkeypatch, {
        "123107.SZ": {"volume": 217, "cost_price": 113.0, "market_value": 24521},
    })
    sig = _mk_signal(["123107"], {"123107": 210}, {"123107": 111.5})
    plan = t.compute_plan("track_cb", sig)
    assert plan["sells"] == []              # 超配7张 < 10 → 死区
    sig2 = _mk_signal([], {}, {})
    plan2 = t.compute_plan("track_cb", sig2)
    assert [e["shares"] for e in plan2["sells"]] == [217]   # 清仓不受死区限制


def _tier(plan, sig):
    from scripts.fetch_and_execute import classify_tier
    return classify_tier(plan, sig)


def test_tier3_triggers():
    from scripts.fetch_and_execute import classify_tier
    sig = _mk_signal([], {}, {})
    plan = {"sells": [{"code": "a", "direction": "sell", "shares": 1000,
                       "est_amount": 120000}], "buys": []}
    tier, reasons = classify_tier(plan, sig)
    assert tier == 3 and any("单笔" in r for r in reasons)
    sig2 = _mk_signal([], {}, {}, buy=[str(i) for i in range(9)])
    tier2, _ = classify_tier({"sells": [], "buys": []}, sig2)
    assert tier2 == 3 and any("新增" in r for r in _)
    # 闲置部署: 非过渡期 + 总>10万
    sig3 = _mk_signal([], {}, {}, interim_deployment=False)
    plan3 = {"sells": [], "buys": [{"code": "x", "direction": "buy",
                                    "shares": 1000, "est_amount": 110000}]}
    tier3, r3 = classify_tier(plan3, sig3)
    assert tier3 == 3 and any("闲置" in r for r in r3)


def test_tier1_and_tier2():
    from scripts.fetch_and_execute import classify_tier
    sig = _mk_signal([], {}, {})
    small = {"sells": [], "buys": [{"code": "123107", "direction": "buy",
                                    "shares": 50, "est_amount": 5575}]}
    assert classify_tier(small, sig)[0] == 1
    mid = {"sells": [{"code": "128134", "direction": "sell", "shares": 190,
                      "est_amount": 23047}], "buys": []}
    assert classify_tier(mid, sig)[0] == 2       # 单笔>2万 → 档2(今日实盘形态)


def test_maturity_band():
    from scripts.cb_signal import maturity_band
    # 剔除线 N=60: 已持有 <60 天 → 卖出
    assert maturity_band(59, True) == "exclude"
    assert maturity_band(59, False) == "exclude"
    # 缓冲带 60~120: 已持有 → 可持有不加仓; 未持有 → 禁买
    assert maturity_band(71, True) == "hold_no_buy"
    assert maturity_band(79, False) == "exclude"
    # 正常
    assert maturity_band(121, False) == "ok"
    assert maturity_band(300, True) == "ok"


def test_monthly_snapshot_trigger():
    from scripts.cb_signal import monthly_snapshot_trigger
    # 2026-10-21 用户批准: 月度=新快照出现（每月1日快照→当月首个交易日）
    assert monthly_snapshot_trigger("2026-10-01", "2026-11-01")
    # 同日重建（周更 append）/快照倒退（数据回滚）都不触发
    assert not monthly_snapshot_trigger("2026-10-01", "2026-10-01")
    assert not monthly_snapshot_trigger("2026-10-01", "2026-10-01 00:00:00")
    assert not monthly_snapshot_trigger("2026-11-01", "2026-10-01")
    # 迁移日/首日: prev 无 snap_date → 当前持仓视为已拍, 不凭空调仓
    assert not monthly_snapshot_trigger(None, "2026-11-01")
    assert not monthly_snapshot_trigger("2026-10-01", None)
