"""测试统一使用 development 环境；需要仿真/生产语义的用例自行 monkeypatch。"""
import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    monkeypatch.setenv("ENV", "development")
