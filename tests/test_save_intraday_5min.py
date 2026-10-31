"""5分钟落库的符号映射与目标集合测试（不联网）。"""
import json

from scripts.save_intraday_5min import _json_holdings, sina_symbol, targets


def test_sina_symbol_handles_index_stock_cb_etf():
    assert sina_symbol("sh000001") == "sh000001"
    assert sina_symbol("000001") == "sz000001"      # 平安银行，不是指数
    assert sina_symbol("600030") == "sh600030"
    assert sina_symbol("000027") == "sz000027"
    assert sina_symbol("110092") == "sh110092"
    assert sina_symbol("113048") == "sh113048"
    assert sina_symbol("123001") == "sz123001"
    assert sina_symbol("127027") == "sz127027"
    assert sina_symbol("588000") == "sh588000"
    assert sina_symbol("159915") == "sz159915"


def test_json_holdings_missing_returns_empty(tmp_path):
    assert _json_holdings(tmp_path / "nope.json") == []


def test_targets_includes_index_strategy_and_cb(tmp_path):
    holdings = tmp_path / "holdings.csv"
    holdings.write_text("code,shares\n600030,100\n110092,10\n400286,100\n",
                        encoding="utf-8")
    g0 = tmp_path / "g0.json"
    g0.write_text(json.dumps({"holdings": ["000001", "600030"]}), encoding="utf-8")
    g1 = tmp_path / "g1.json"
    g1.write_text(json.dumps({"holdings": ["123001"]}), encoding="utf-8")
    cb = tmp_path / "cb.json"
    cb.write_text(json.dumps({"holdings": ["110092", "113048"]}), encoding="utf-8")
    got = set(targets(holdings_csv=holdings, signal_files=(g0, g1, cb)))
    assert got == {"sh000001", "000001", "600030", "123001", "110092", "113048"}
    assert "400286" not in got
