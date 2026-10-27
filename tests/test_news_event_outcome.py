import pandas as pd
import pytest

from scripts.news_event_outcome import next_trading_day, score_event


def _bars(dates, opens, closes):
    return pd.DataFrame({"date": pd.to_datetime(dates),
                         "open": opens, "close": closes})


def test_next_trading_day_friday_to_monday():
    days = [pd.Timestamp(x) for x in
            ("2026-10-30", "2026-11-02", "2026-11-03")]
    assert next_trading_day(pd.Timestamp("2026-10-30"), days) == pd.Timestamp("2026-11-02")


def test_next_trading_day_weekend():
    days = [pd.Timestamp(x) for x in ("2026-10-30", "2026-11-02")]
    assert next_trading_day(pd.Timestamp("2026-10-31"), days) == pd.Timestamp("2026-11-02")


def test_t1_gap_and_returns_use_next_open():
    bars = _bars(
        ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
         "2026-10-30", "2026-11-02"],
        [100, 102, 104, 103, 105, 108],
        [101, 103, 105, 104, 106, 109],
    )
    r = score_event("2026-10-26 11:29:02", bars)
    assert r["status"] == "scored_5d"
    assert r["entry_date"] == "2026-10-27"
    assert r["gap_pct"] == pytest.approx(round((102 / 101 - 1) * 100, 3), abs=1e-6)
    assert r["intraday_pct"] == pytest.approx(round((103 / 102 - 1) * 100, 3), abs=1e-6)
    assert r["ret_1d_pct"] == pytest.approx(round((103 / 101 - 1) * 100, 3), abs=1e-6)
    # 5d: 2026-11-02 close / 2026-10-27 open
    assert r["ret_5d_pct"] == pytest.approx(round((109 / 102 - 1) * 100, 3), abs=1e-6)
    assert r["ret_20d_pct"] is None


def test_event_after_friday_enters_monday():
    bars = _bars(["2026-10-30", "2026-11-02", "2026-11-03"],
                 [100, 105, 106], [101, 106, 107])
    r = score_event("2026-10-30 20:16:47", bars)
    assert r["entry_date"] == "2026-11-02"
    assert r["gap_pct"] == pytest.approx(round((105 / 101 - 1) * 100, 3), abs=1e-6)


def test_no_future_bar_is_pending_not_zero():
    bars = _bars(["2026-10-26"], [100], [101])
    r = score_event("2026-10-26 12:00:00", bars)
    assert r["status"] == "pending_entry"
    assert "ret_1d_pct" not in r


def test_bad_time_is_explicit():
    bars = _bars(["2026-10-26"], [100], [101])
    assert score_event("not-a-time", bars)["status"] == "bad_time"
