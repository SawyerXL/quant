"""负持仓导出拆分测试（2026-10-21 残差审计待办 2）。

背景：002028 −399 股被旧导出 `volume>0` 静默过滤，快照/对账全链对负持仓
盲。执行层已按 0 持仓隔离；本测试锁住"负持仓单独落盘、不得丢弃"。
运行：.venv/bin/python -m pytest tests/test_export_positions.py -v
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_positions", ROOT / "scripts" / "export_qmt_positions.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_split_negative_kept_and_suffix_stripped():
    pos, neg = mod.split_positions({
        "002028.SZ": {"volume": -399, "cost_price": 204.077,
                      "market_value": -44923.0},
        "113059.SH": {"volume": 200, "cost_price": 115.9016,
                      "market_value": 22971.6},
        "000001.SZ": {"volume": 0, "cost_price": 0, "market_value": 0},
    })
    assert set(pos) == {"113059"}
    assert set(neg) == {"002028"}
    assert neg["002028"]["volume"] == -399
    assert neg["002028"]["market_value"] == -44923.0
    assert pos["113059"]["volume"] == 200


def test_split_empty():
    pos, neg = mod.split_positions({})
    assert pos == {} and neg == {}
