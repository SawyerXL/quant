"""仿真/生产环境 QMT 不可用时不得静默回退 Mock。"""
import pytest

import execution.qmt_client as qc


def test_simulation_without_xtquant_fails(monkeypatch):
    monkeypatch.setattr(qc, "QMT_AVAILABLE", False)
    monkeypatch.setenv("ENV", "simulation")
    with pytest.raises(RuntimeError, match="拒绝回退"):
        qc.get_client()


def test_production_without_xtquant_fails(monkeypatch):
    monkeypatch.setattr(qc, "QMT_AVAILABLE", False)
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError, match="拒绝回退"):
        qc.get_client()


def test_development_still_allows_mock(monkeypatch):
    monkeypatch.setattr(qc, "QMT_AVAILABLE", False)
    monkeypatch.setenv("ENV", "development")
    assert isinstance(qc.get_client(), qc.MockQMTClient)


def test_env_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(qc, "QMT_AVAILABLE", False)
    monkeypatch.setenv("ENV", "SIMULATION")
    with pytest.raises(RuntimeError, match="拒绝回退"):
        qc.get_client()


def test_builtin_only_flag_blocks_external_client(monkeypatch, tmp_path):
    flag = tmp_path / "builtin_only.flag"
    flag.write_text("builtin", encoding="utf-8")
    monkeypatch.setattr(qc, "QMT_AVAILABLE", True)
    monkeypatch.setattr(qc, "BUILTIN_ONLY_FLAG", flag)
    monkeypatch.setenv("ENV", "simulation")
    with pytest.raises(RuntimeError, match="内置 Python"):
        qc.get_client()


def test_env_trailing_space_does_not_fall_back_to_mock(monkeypatch):
    # Windows `set ENV=simulation &&` 的尾部空格；10-29 002028 实锤
    monkeypatch.setattr(qc, "QMT_AVAILABLE", False)
    monkeypatch.setenv("ENV", "simulation ")
    with pytest.raises(RuntimeError, match="拒绝回退"):
        qc.get_client()
