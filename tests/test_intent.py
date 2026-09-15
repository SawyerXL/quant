"""
Intent 层隔离自测（2026-09-15）——本周状态：只测层本身，不接实盘路径。
用例按本周实锤事故回放构造：09-14 假深熊清仓卖 511360（豁免未传导）、
688825 隔离泄漏、同标的反向意图冲突。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.intent import Intent, IntentRouter


def test_sweep_owns_bond_etf():
    """sweep 属主可操作自己的标的。"""
    r = IntentRouter()
    d = r.submit(Intent(source="sweep", code="511360", direction="buy",
                        qty=6100, price=114.128))
    assert d.ok, d.reason


def test_bear_sell_of_sweep_asset_refused():
    """09-14 事故回放：trader 熊市清仓卖 sweep 属主的 511360 → 拒绝。"""
    r = IntentRouter()
    r.submit(Intent(source="sweep", code="511360", direction="buy",
                    qty=6100, price=113.9))
    d = r.submit(Intent(source="track_a", code="511360", direction="sell",
                        qty=6100, price=111.63))
    assert not d.ok
    assert "豁免" in d.reason and "sweep" in d.reason


def test_quarantine_refused_for_all_sources():
    """688825 泄漏教训：隔离标的任何来源都不得操作。"""
    r = IntentRouter()
    for src in ("track_a", "track_cb", "sweep", "manual"):
        d = r.submit(Intent(source=src, code="688825", direction="buy",
                            qty=100, price=50.0))
        assert not d.ok and "隔离" in d.reason, (src, d.reason)


def test_reverse_intent_conflict():
    """同标的近窗反向意图（不同来源）→ P0 拒绝，含被拒意图作证据。"""
    r = IntentRouter()
    d1 = r.submit(Intent(source="track_a", code="600276", direction="sell",
                         qty=1000, price=40.0))
    assert d1.ok
    d2 = r.submit(Intent(source="manual", code="600276", direction="buy",
                         qty=1000, price=40.0))
    assert not d2.ok and "反向意图冲突" in d2.reason


def test_same_direction_no_conflict():
    """同向意图不冲突（分批）。"""
    r = IntentRouter()
    assert r.submit(Intent(source="track_a", code="600276", direction="sell",
                           qty=500, price=40.0)).ok
    assert r.submit(Intent(source="track_a", code="600276", direction="sell",
                           qty=500, price=40.0)).ok


def test_normal_stock_flow():
    """普通股票路径不受影响。"""
    r = IntentRouter()
    assert r.submit(Intent(source="track_a", code="600030", direction="buy",
                           qty=1000, price=27.0)).ok
