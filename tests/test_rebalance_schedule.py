"""调仓日程唯一实现测试(2026-10-19 三原则裁定 b)。

锁死实盘语义: 月中=n//2−1(9-14/10-19), 月末=倒数第二个交易日(9-29/10-29);
三处调用必须一致(本测试同时校验 A2/引擎委托到共享实现)。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rebalance_schedule import (biweekly_dates, is_rebalance_day,
                                month_end_index, month_mid_index)


def _cal(start="2026-01-01", end="2026-12-31"):
    # 用真实交易日历
    from data.storage import load_meta
    c = sorted(load_meta("trade_calendar")["trade_date"].astype(str).tolist())
    return [d for d in c if start <= d <= end]


def test_month_mid_index():
    # 9月21天→9; 10月17天→7
    assert month_mid_index(21) == 9
    assert month_mid_index(17) == 7
    assert month_mid_index(2) == 0


def test_known_2026_dates():
    cal = _cal()
    assert is_rebalance_day("2026-09-14", cal) is True
    assert is_rebalance_day("2026-09-15", cal) is False
    assert is_rebalance_day("2026-09-29", cal) is True
    assert is_rebalance_day("2026-10-19", cal) is True
    assert is_rebalance_day("2026-10-20", cal) is False
    assert is_rebalance_day("2026-10-29", cal) is True
    assert is_rebalance_day("2026-10-30", cal) is False


def test_biweekly_dates_include_mid_and_second_last():
    cal = _cal()
    by_month = {}
    for d in cal:
        by_month.setdefault(d[:7], []).append(d)
    for ym, mds in by_month.items():
        out = [d for d in biweekly_dates(cal) if d.startswith(ym)]
        expect = sorted({mds[month_mid_index(len(mds))],
                         mds[month_end_index(len(mds))]})
        assert out == expect, ym


def test_three_call_sites_agree():
    """实盘链/A2/引擎 三处必须逐日一致(统一来源回归)。"""
    cal = _cal()
    from daily_signal_a_v2 import is_rebalance_day as live
    from run_backtest_a2 import _make_rebal_dates as a2
    from backtest_engine import make_rebal_dates as eng

    live_days = [d for d in cal if live(d, cal)]
    a2_days = [d for d in a2(cal, "biweekly") if d >= cal[0]]
    eng_days = [d for d in eng(cal, "biweekly") if d >= cal[0]]
    assert live_days == a2_days, (len(live_days), len(a2_days))
    assert live_days == eng_days, (len(live_days), len(eng_days))
