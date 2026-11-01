"""执行链 profile 路径测试：默认仿真不变，live 可切远程根/信号目录。"""
from scripts import fetch_and_execute as fe


def test_default_profile_is_simulation_paths():
    assert fe.ACCOUNT_PROFILE == "sim"
    assert fe.LINUX_REMOTE_ROOT == "/root/quant"
    assert fe.LOCAL_SIGNAL_DIR == "data_store/meta"
    assert fe.REMOTE_SIGNAL_DIR == "data_store/meta"
    assert fe.remote_signal_path("signal_a_g0.json") == "data_store/meta/signal_a_g0.json"
    assert str(fe.local_signal_path("signal_a_g0.json")).endswith(
        "data_store/meta/signal_a_g0.json"
    )


def test_live_profile_remote_and_local_are_separate(monkeypatch):
    monkeypatch.setattr(fe, "LINUX_REMOTE_ROOT", "/root/quant_live")
    monkeypatch.setattr(fe, "REMOTE_SIGNAL_DIR", "signals")
    monkeypatch.setattr(fe, "LOCAL_SIGNAL_DIR", "data_store/meta")
    assert fe.remote_signal_path("signal_cb_latest.json") == "signals/signal_cb_latest.json"
    assert str(fe.local_signal_path("signal_cb_latest.json")).endswith(
        "data_store/meta/signal_cb_latest.json"
    )
    # 远端 URL 由函数拼接；这里验证根与相对路径不会串
    assert f"{fe.LINUX_REMOTE_ROOT}/{fe.remote_signal_path('x.json')}" == \
        "/root/quant_live/signals/x.json"
