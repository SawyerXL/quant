from scripts.check_execution_chain import evaluate


TODAY = "20261027"


def _sig(**kw):
    d = {"signal_date": "2026-10-27", "buy": [], "sell": [], "rebalance": False}
    d.update(kw)
    return d


def test_non_rebalance_day_no_result_required():
    issues, warnings, notes = evaluate(
        TODAY, {"a": _sig(), "cb": _sig()}, {})
    assert issues == []
    assert warnings == []
    assert len(notes) == 2


def test_cb_rebalance_without_result_is_issue():
    issues, _, _ = evaluate(
        TODAY, {"a": _sig(), "cb": _sig(rebalance=True, buy=["123107"])}, {})
    assert any("cb" in x and "execution_result" in x for x in issues)


def test_cb_result_present_is_ok():
    res = {"signal_date": "20261027", "fill_rate_pct": 100,
           "blocked": [], "skipped_rejected": []}
    issues, warnings, _ = evaluate(
        TODAY, {"a": _sig(), "cb": _sig(rebalance=True, buy=["123107"])},
        {"cb": res})
    assert issues == []
    assert warnings == []


def test_stock_buy_without_result_is_issue():
    issues, _, _ = evaluate(
        TODAY, {"a": _sig(buy=["600030"]), "cb": _sig()}, {})
    assert any(x.startswith("a:") and "execution_result" in x for x in issues)


def test_stale_signal_is_issue():
    issues, _, _ = evaluate(
        TODAY, {"a": _sig(signal_date="2026-10-26"), "cb": _sig()}, {})
    assert any("signal_date=20261026" in x for x in issues)


def test_partial_fill_is_warning():
    res = {"signal_date": "20261027", "fill_rate_pct": 80,
           "blocked": [], "skipped_rejected": []}
    issues, warnings, _ = evaluate(
        TODAY, {"a": _sig(), "cb": _sig(rebalance=True, buy=["123107"])},
        {"cb": res})
    assert issues == []
    assert any("fill_rate=80" in x for x in warnings)


def test_stock_date_mismatch_is_issue():
    issues, _, _ = evaluate(
        TODAY, {"a": {"signal_date": "MISMATCH", "buy": ["x"], "sell": []},
                "cb": _sig()}, {})
    assert issues
