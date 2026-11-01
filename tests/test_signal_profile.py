"""信号输出 profile：默认 data_store/meta；live 由 SIGNAL_DIR 切到独立目录。"""
from scripts import cb_signal, daily_signal_a_grouped, daily_signal_a_v2


def test_default_signal_paths_are_simulation():
    assert str(cb_signal.SIGNAL_FILE).endswith("data_store/meta/signal_cb_latest.json")
    assert str(daily_signal_a_grouped._BASE).endswith("data_store/meta")
    assert str(daily_signal_a_grouped.LEGACY_FILE).endswith(
        "data_store/meta/signal_a_latest.json"
    )
    assert str(daily_signal_a_v2.SIGNAL_FILE).endswith(
        "data_store/meta/signal_a_latest.json"
    )
