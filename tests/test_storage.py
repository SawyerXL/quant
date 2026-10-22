"""save_daily 跳变守卫测试: 份额折算豁免 + 污染拒绝不回归。

背景(2026-09-14): 510500 自 09-06 守卫上线起每日被拒 2772~2777 行、一根
新 bar 都没进库——2015-04 真实一拆3.5 毒化游标后连拒全部后续行。
"""
import pandas as pd
import pytest

from data import storage


def _mk(codes_dates_closes):
    """造库: {year: [(date_str, close)]}。codes_dates_closes = {code: {year: [(date, close)]}}"""
    for code, years in codes_dates_closes.items():
        for year, rows in years.items():
            pd.DataFrame({"date": [r[0] for r in rows],
                          "close": [r[1] for r in rows]}).to_parquet(
                storage._daily_path(code, year), index=False)


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_STORE", tmp_path)
    return tmp_path


def test_split_row_accepted_and_cursor_continues(tmp_store):
    # 库内已有含折算的历史: 2015-04-10 close 2.243 → 2015-04-15 close 7.818(×3.5)
    _mk({"510500": {
        2015: [("2015-04-10", 2.243), ("2015-04-15", 7.818), ("2015-04-16", 7.887)],
        2016: [("2016-01-04", 7.200)],
    }})
    # 入参=新浪全history: 含同一折算 + 两根新 bar
    df = pd.DataFrame({
        "date": ["2015-04-10", "2015-04-15", "2015-04-16", "2016-01-04", "2016-01-05", "2016-01-06"],
        "close": [2.243, 7.818, 7.887, 7.200, 7.100, 7.150],
    })
    rejected = storage.save_daily("510500", df)
    assert rejected == 0, f"折算行与后续行不应被拒, 实际拒 {rejected}"
    out = pd.read_parquet(storage._daily_path("510500", 2016))
    dates = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d").tolist()
    assert dates == ["2016-01-04", "2016-01-05", "2016-01-06"]


def test_polluted_row_still_rejected(tmp_store):
    # 污染行=库内无该日期(新bar)且相对前收跳>50%
    _mk({"600000": {2016: [("2016-01-04", 10.0)]}})
    df = pd.DataFrame({
        "date": ["2016-01-05"],
        "close": [99.0],  # 10→99 = +890%, 库内无 01-05 锚
    })
    rejected = storage.save_daily("600000", df)
    assert rejected == 1
    out = pd.read_parquet(storage._daily_path("600000", 2016))
    assert list(out["date"]) == ["2016-01-04"]


def test_load_daily_drops_zero_close(tmp_store):
    # 2026-10-22: 零价占位行(000016 *ST康佳 2026-09-24)不得进入消费方
    _mk({"000016": {2026: [("2026-09-03", 2.46), ("2026-09-24", 0.0),
                           ("2026-09-25", float("nan"))]}})
    out = storage.load_daily("000016", "2026-01-01", "2026-12-31")
    assert list(out["close"]) == [2.46]


def test_jump_where_store_disagrees_still_rejected(tmp_store):
    # 库内同日期存在但与入参不一致(真污染): 锚豁免不得放行
    _mk({"600001": {2016: [("2016-01-04", 10.0), ("2016-01-05", 10.1)]}})
    df = pd.DataFrame({
        "date": ["2016-01-05"],
        "close": [88.8],  # 库内 01-05=10.1, 入参 88.8 → 不一致, 拒
    })
    rejected = storage.save_daily("600001", df)
    assert rejected == 1
    out = pd.read_parquet(storage._daily_path("600001", 2016))
    assert list(out["date"]) == ["2016-01-04", "2016-01-05"]
    assert list(out["close"]) == [10.0, 10.1]  # 库内原值未被污染覆盖
