"""快照链 profile：默认仍指向仿真 H:/quant，live 可传 H:/quant_live。"""
from scripts import qmt_snapshot as snap


def test_default_snapshot_root_is_simulation():
    assert snap.WIN_ROOT == "H:/quant"
    assert snap.WIN_FILE == "H:/quant/logs/qmt_positions_latest.json"
    assert snap.WIN_ROOT_WIN == "H:\\quant"


def test_live_snapshot_root_builder():
    assert snap.win_file_for("H:/quant_live") == \
        "H:/quant_live/logs/qmt_positions_latest.json"
    assert "H:/quant_live".replace("/", "\\") == "H:\\quant_live"
