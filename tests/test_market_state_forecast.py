"""状态预测台账：非交易日/数据未更新时不得登记（防节假日重复行）。"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import market_state_forecast as m


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
