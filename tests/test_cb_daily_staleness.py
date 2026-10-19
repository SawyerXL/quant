"""CB 日更陈旧判据测试(2026-10-19)。

锁死: "最近交易日"取自 trade_calendar, 非日历日推算; 日更按它判陈旧。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from build_cb_data import _latest_trading_day
from data.storage import load_meta


def test_latest_trading_day_in_calendar_and_not_future():
    d = _latest_trading_day()
    cal = set(load_meta("trade_calendar")["trade_date"].astype(str).str[:10])
    assert d in cal
    assert d <= date.today().strftime("%Y-%m-%d")
    # 与日历中 <= 今天的最大日期一致
    le = sorted(x for x in cal if x <= date.today().strftime("%Y-%m-%d"))
    assert d == le[-1]
