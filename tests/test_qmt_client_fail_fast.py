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
