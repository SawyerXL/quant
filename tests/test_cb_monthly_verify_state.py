"""CB 月度验证器状态落盘（2026-10-23）：非调仓日"跳过"也必须可被健康行看见。

闸门十二：正确跳过是最坏的静默形态——验证器死了和"今天不该调仓"输出相同。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import verify_cb_monthly_rebalance as v


def test_skip_writes_state(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(v, "expected_rebalance_day", lambda snap: "2026-11-02")
    monkeypatch.setattr(sys, "argv", ["verify", "--date", "2026-10-23"])
    rc = v.main()
    assert rc == 0
    d = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert d["status"] == "skipped"
    assert d["date"] == "2026-10-23"
    assert d["expect_snap"] == "2026-10-01"
    assert d["expect_day"] == "2026-11-02"


def test_next_expect_day(monkeypatch):
    days = {"2026-10-01": "2026-10-08", "2026-11-01": "2026-11-02"}
    monkeypatch.setattr(v, "expected_rebalance_day", lambda s: days.get(s[:10]))
    # 本月调仓日还没到 → 返回本月
    assert v.next_expect_day("2026-10-08", "2026-10-05", "2026-10-01") == "2026-10-08"
    # 本月已过 → 返回下月快照日的执行日
    assert v.next_expect_day("2026-10-08", "2026-10-23", "2026-10-01") == "2026-11-02"


def test_write_state_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "STATE", tmp_path / "state.json")
    v._write_state(date="2026-11-02", status="pass", n_cb=25)
    d = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert d["status"] == "pass"
    assert d["n_cb"] == 25
    assert d["ts"]
