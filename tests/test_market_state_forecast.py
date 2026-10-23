"""状态预测台账：非交易日/数据未更新时不得登记（防节假日重复行）。"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import market_state_forecast as m


def _price_from_rets(rets):
    idx = pd.bdate_range("2020-01-01", periods=len(rets))
    return pd.Series(100 * (1 + pd.Series(rets, index=idx)).cumprod(), index=idx)


def test_c1_only_when_today_is_big_move():
    # 350 个历史大波动日（|ret|>=1%），但今天 +0.2% → 不得选 C1
    rets = [0.012 if i % 2 == 0 else 0.002 for i in range(700)]
    s = _price_from_rets(rets)
    p, n, cond, base = m._conditional(s, s.index[-1], 0.01)
    assert cond == "C2_vol20_bin"

    # 今天 +1.5% → C1 生效
    rets[-1] = 0.015
    s = _price_from_rets(rets)
    p2, n2, cond2, base2 = m._conditional(s, s.index[-1], 0.01)
    assert cond2 == "C1_big_move"


def test_score_skips_invalid_rows(tmp_path, monkeypatch):
    s = _price_from_rets([0.0, 0.01, 0.0])
    monkeypatch.setattr(m, "_series", lambda: s)
    p = tmp_path / "ledger.csv"
    rows = [
        {"registered_at": "2026-01-01T18:20:00", "market_date": "2020-01-01",
         "event": "abs_ret_ge_1pct", "p_event": 0.3, "base_rate": 0.25,
         "n_cond": 500, "condition": "C2_vol20_bin", "features": "{}",
         "outcome_event": "", "scored_at": "", "valid": "invalid_c1_bug"},
        {"registered_at": "2026-01-01T18:20:00", "market_date": "2020-01-01",
         "event": "abs_ret_ge_1.5pct", "p_event": 0.1, "base_rate": 0.1,
         "n_cond": 500, "condition": "C2_vol20_bin", "features": "{}",
         "outcome_event": "", "scored_at": "", "valid": "ok"},
    ]
    pd.DataFrame(rows).to_csv(p, index=False)
    monkeypatch.setattr(m, "LEDGER", p)
    m.score()
    d = pd.read_csv(p)
    assert pd.isna(d.loc[0, "outcome_event"])       # invalid 不结算
    assert d.loc[1, "outcome_event"] == 0.0         # valid 正常结算


def test_register_skips_when_latest_bar_not_today(tmp_path, monkeypatch):
    s = pd.Series(
        [1.0, 2.0, 3.0],
        index=pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
    )
    class _FixedDate:
        @staticmethod
        def today():
            return pd.Timestamp("2026-10-23").date()

    monkeypatch.setattr(m, "_series", lambda: s)
    monkeypatch.setattr(m, "date", _FixedDate)
    monkeypatch.setattr(m, "LEDGER", tmp_path / "ledger.csv")
    m.register()
    assert not (tmp_path / "ledger.csv").exists()
