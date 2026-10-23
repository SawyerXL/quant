"""分组信号日程必须与唯一日程 rebalance_schedule 同源（2026-10-23 修复）。

背景：10-19 日程三原则统一了 v2/A2/引擎，但漏了 daily_signal_a_grouped；
旧实现是字面15日+自然月末，且 15 日休市时整月缺月中锚点（2026-11-15 周日）。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import daily_signal_a_grouped as g
from data.storage import load_meta
from rebalance_schedule import biweekly_dates


def _cal():
    cal = load_meta("trade_calendar")
    return sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d"))


def test_base_anchors_match_canonical():
    cal = _cal()
    base = [d for d in cal if g._is_base_rebalance_day(d, cal)]
    canon = [d for d in cal if d in set(biweekly_dates(cal))]
    assert base == canon


def test_november_mid_month_anchor_not_swallowed():
    cal = _cal()
    anchors = [d for d in cal
               if d.startswith("2026-11") and g._is_base_rebalance_day(d, cal)]
    # 11-15 是周日；旧实现只有 11-30，新实现必须有 11-13/11-27
    assert anchors == ["2026-11-13", "2026-11-27"]


def test_group_offsets_on_canonical_anchors():
    cal = _cal()
    assert g._is_group_rebalance_day("2026-11-17", cal, 2)   # 11-13 +2
    assert g._is_group_rebalance_day("2026-11-24", cal, 7)   # 11-13 +7
    # 旧字面15日锚点(10-15)的 g1=10-26 不再是调仓日
    assert not g._is_group_rebalance_day("2026-10-26", cal, 7)
