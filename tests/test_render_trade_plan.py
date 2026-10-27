from scripts.render_trade_plan import build_rows


def test_sells_before_buys_and_amounts():
    plan = {
        "sells": [{"code": "110081", "direction": "sell", "shares": 220,
                   "price": 101.2, "est_amount": 22264.0, "held": 220, "target": 0}],
        "buys": [{"code": "113049", "direction": "buy", "shares": 210,
                  "price": 106.1, "est_amount": 22281.0, "held": 0, "target": 210}],
    }
    rows = build_rows(plan)
    assert [r["side"] for r in rows] == ["sell", "buy"]
    assert rows[0]["flags"] == ""
    assert rows[1]["flags"] == ""


def test_buy_lot_and_order_cap_flags():
    plan = {"sells": [], "buys": [
        {"code": "123107", "shares": 205, "price": 100.0,
         "est_amount": 20_500.0},
        {"code": "123108", "shares": 1000, "price": 120.0,
         "est_amount": 120_000.0},
    ]}
    rows = build_rows(plan)
    assert "买入非10张整手" in rows[0]["flags"]
    assert "单笔>10万需拆" in rows[1]["flags"]


def test_empty_plan():
    assert build_rows({}) == []
