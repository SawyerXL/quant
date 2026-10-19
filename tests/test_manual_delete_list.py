"""删笔清单测试(2026-10-14, 十二/十三/十六轮规格; 含走查两用例)。

走查用例(十六轮锁定): ①合成删笔条目→确认被排除不进残差 ②合成强赎券
(在清单内)→确认告警而非静默排除(强制退出优先于清单)。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.manual_delete_list import (DELETE_LIST_PATH,  # noqa
                                          generate_from_deletions,
                                          load_list, suppression_sets)


def _write_list(tmp, entries, valid_until=None):
    dl = {"valid_until": valid_until or (date.today() + timedelta(days=7)).isoformat(),
          "entries": entries}
    p = tmp / "manual_delete_list.json"
    p.write_text(json.dumps(dl, ensure_ascii=False), encoding="utf-8")
    return p


# ── 模块单测 ────────────────────────────────────────────────

def test_load_missing_returns_empty(tmp_path):
    assert load_list(tmp_path / "nope.json") == {"valid_until": "", "entries": []}


def test_load_expired_ignored(tmp_path):
    p = _write_list(tmp_path, [{"code": "127038", "direction": "buy"}],
                    valid_until="2020-01-01")
    out = load_list(p)
    assert out["entries"] == []


def test_suppression_sets_and_override():
    dl = {"entries": [
        {"code": "127038", "direction": "buy", "reason": "事件复核删除"},
        {"code": "128134", "direction": "sell", "reason": "暂不卖"},
        {"code": "128142", "direction": "sell", "reason": "临期示例"},
    ]}
    ss, sb, ov = suppression_sets(dl, forced_exit_codes={"128142"})
    assert ss == {"128134"} and sb == {"127038"}
    assert [o["code"] for o in ov] == ["128142"]


def test_generate_from_deletions():
    dl = generate_from_deletions(
        [{"code": "127038.SZ", "direction": "buy", "reason": "删"}],
        valid_until="2026-10-29")
    e = dl["entries"][0]
    assert e["code"] == "127038" and e["deleted_at"] == date.today().isoformat()
    with pytest.raises(ValueError):
        generate_from_deletions([{"code": "x", "direction": "hold"}], "2026-10-29")


# ── 走查用例(compute_plan 级) ────────────────────────────────

def _trader_with_positions(monkeypatch, positions):
    from execution.trader import Trader
    from execution.qmt_client import get_client
    client = get_client()
    client._positions = positions
    monkeypatch.setattr(Trader, "__init__",
                        lambda self: setattr(self, "client", client))
    return Trader()


def _mk_signal(holdings, shares, prices, excluded=None, **kw):
    sig = {"signal_date": "2026-10-20", "track": "cb", "capital": 600000,
           "holdings": holdings, "shares": shares, "prices": prices,
           "buy": [], "sell": [], "excluded": excluded or {}}
    sig.update(kw)
    return sig


def test_walkthrough_case1_delete_not_restored(monkeypatch, tmp_path):
    """走查①: 合成删笔条目→确认被排除不进残差(双向)。"""
    from execution import manual_delete_list as mdl
    p = _write_list(tmp_path, [
        {"code": "127038", "direction": "buy", "reason": "合成删买"},
        {"code": "128134", "direction": "sell", "reason": "合成删卖"},
    ])
    monkeypatch.setattr(mdl, "DELETE_LIST_PATH", p)
    t = _trader_with_positions(monkeypatch, {
        "128134.SZ": {"volume": 190, "cost_price": 121.3, "market_value": 23047},
    })
    sig = _mk_signal(["127038"], {"127038": 210}, {"127038": 111.5})
    plan = t.compute_plan("track_cb", sig)
    # 删掉的买单不自动补(无清单时 127038 会进 buys)
    assert all(e["code"] != "127038" for e in plan["buys"])
    # 删掉的卖单不被自动卖(无清单时 128134 清仓进 sells)
    assert all(e["code"] != "128134" for e in plan["sells"])
    skipped = {(e["code"], e["direction"]) for e in plan["skipped_deleted"]}
    assert skipped == {("127038", "buy"), ("128134", "sell")}


def test_walkthrough_case2_forced_exit_overrides(monkeypatch, tmp_path):
    """走查②: 合成强赎券(在清单内)→不静默排除, 卖出照常进计划。"""
    from execution import manual_delete_list as mdl
    p = _write_list(tmp_path, [
        {"code": "128142", "direction": "sell", "reason": "合成删卖"},
    ])
    monkeypatch.setattr(mdl, "DELETE_LIST_PATH", p)
    t = _trader_with_positions(monkeypatch, {
        "128142.SZ": {"volume": 136, "cost_price": 112.4, "market_value": 15286},
    })
    sig = _mk_signal([], {}, {},
                     excluded={"128142": "临期(交易末日2026-12-08, 剩57天)"})
    plan = t.compute_plan("track_cb", sig)
    sells = [e["code"] for e in plan["sells"]]
    assert "128142" in sells                      # 强制退出优先, 不抑制
    assert all(e["code"] != "128142" for e in plan["skipped_deleted"])
