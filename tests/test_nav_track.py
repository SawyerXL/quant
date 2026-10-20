"""NAV 追踪分腿口径测试（2026-10-20）。

背景：旧 build_nav 用 100 万单一 notional 但把 CB 腿持仓也并进 P&L，
invested_pct>100%，股票腿 NAV 被 CB 盈亏污染。修正后：
  nav/nav_a = 股票腿（1M，剔除 CB）
  nav_cb = CB 腿（600k）
  nav_combined = (equity_a+equity_cb)/1.6M
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.qmt_nav_track as nav_mod


def _snap(dirp, d8, exported, pos):
    data = {"exported_at": exported,
            "positions": {c: {"volume": v, "market_value": v * px}
                          for c, (v, px) in pos.items()}}
    (dirp / f"qmt_positions_{d8}.json").write_text(
        json.dumps(data), encoding="utf-8")


def test_sleeve_navs_and_combined(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_mod, "SNAP_DIR", tmp_path)
    monkeypatch.setattr(nav_mod, "CASH_YIELD", 0.0)
    monkeypatch.setattr(nav_mod, "COMMISSION", 0.0)
    _snap(tmp_path, "20261020", "2026-10-20T15:05:00",
          {"511360": (1000, 100.0), "113000": (100, 1000.0)})
    _snap(tmp_path, "20261021", "2026-10-21T15:05:00",
          {"511360": (1000, 110.0), "113000": (100, 1100.0)})
    df = nav_mod.build_nav()
    assert len(df) == 2
    r = df.iloc[-1]
    assert r["nav_a"] == pytest.approx(1.01, abs=1e-9)
    assert r["nav_cb"] == pytest.approx(1.0 + 10000 / 600000, abs=1e-9)
    assert r["nav_combined"] == pytest.approx(1.0 + 20000 / 1600000, abs=1e-9)
    assert r["nav"] == pytest.approx(r["nav_a"], abs=1e-12)
    assert r["n_pos_a"] == 1 and r["n_pos_cb"] == 1
    assert r["invested_pct"] < 100  # 股票腿 invested/equity，不再被 CB 撑到 >100%
    assert r["ret_combined"] == pytest.approx(0.0125, abs=1e-9)
