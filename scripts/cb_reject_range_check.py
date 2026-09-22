"""废单调查对照表: 09-21 全部23笔CB委托价 vs 当日区间/昨收, 并与09-01同券对照。
输出行: code | 09-21委托价 | 09-21状态 | vs当日高% | vs当日收% | vs昨收% | 09-01同价委托是否成交过
"""
import json
from pathlib import Path

import pandas as pd

CB_DAILY = Path("data_store/convertible_bonds/daily")
DAYS = ["2026-08-29", "2026-09-01", "2026-09-18", "2026-09-21"]


def load_bar(code: str, day: str) -> dict | None:
    f = CB_DAILY / f"{code}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    date_col = "date" if "date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
    row = df[df[date_col] == day]
    if row.empty:
        return None
    r = row.iloc[0]
    return {"open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"])}


def load_orders_0921() -> list:
    d = json.loads(Path("logs/qmt_orders_20260921.json").read_text())
    out = []
    for o in d["orders"]:
        out.append({"code": o["code"], "direction": o["direction"],
                    "shares": o["shares"], "price": float(o["limit_price"]),
                    "status": o["status"], "filled": o["filled"]})
    return out


def load_orders_0901() -> list:
    d = json.loads(Path("logs/qmt_positions_20260901.json").read_text())
    out = []
    for o in d.get("today_orders", []):
        out.append({"code": o["code"].split(".")[0], "direction": o["direction"],
                    "shares": o["shares"], "price": float(o["price"]),
                    "status": o["status"], "filled": o["filled"]})
    return out


def pct(a, b):
    return (a / b - 1) * 100


def fmt(x, w=9):
    if x is None:
        return f"{'-':>{w}}"
    return f"{round(x, 2):>{w}}"


def main():
    orders_21 = load_orders_0921()
    orders_01 = load_orders_0901()
    # 09-01 各code是否有过同委托价的成交(status 56)
    filled_prices_01 = {}
    for o in orders_01:
        if o["status"] == 56 and o["direction"] == "buy":
            filled_prices_01.setdefault(o["code"], set()).add(
                round(o["price"], 3))

    print("=" * 100)
    print("09-21 全部订单 (委托价 vs 09-21当日区间/09-18昨收; 右列为09-01同委托价是否成交过)")
    print("=" * 100)
    hdr = (f"{'code':8}{'dir':4}{'张':>5}{'委托价':>10}{'状态':>4}{'成交':>5} "
           f"{'vs当日高%':>9}{'vs当日收%':>9}{'vs昨收%':>8}  {'09-21bar':>8} "
           f"{'09-01同价成过':>10}")
    print(hdr)
    for o in orders_21:
        code = o["code"]
        bar21 = load_bar(code, "2026-09-21")
        bar18 = load_bar(code, "2026-09-18")
        if bar21:
            vsh = pct(o["price"], bar21["high"])
            vsc = pct(o["price"], bar21["close"])
            barb = "有"
        else:
            vsh = vsc = None
            barb = "无!!"
        if bar18:
            vsp = pct(o["price"], bar18["close"])
        else:
            vsp = None
        was = "是" if o["price"] in filled_prices_01.get(code, set()) else "否"
        s21 = f"{code:8}{o['direction']:4}{o['shares']:>5}{o['price']:>10.3f}" \
              f"{o['status']:>4}{o['filled']:>5}" + \
              fmt(vsh) + fmt(vsc) + fmt(vsp, 8) + f"  {barb:>8}  {was:>10}"
        print(s21)

    print()
    print("=" * 100)
    print("09-01 全部CB订单对照 (委托价 vs 09-01当日区间/08-29昨收) — 只看买方向")
    print("=" * 100)
    print(f"{'code':8}{'张':>5}{'委托价':>10}{'状态':>4}{'成交':>5} "
          f"{'vs当日高%':>9}{'vs当日收%':>9}{'vs昨收%':>8}  {'09-01bar':>8}")
    seen = set()
    for o in orders_01:
        if o["direction"] != "buy":
            continue
        code = o["code"]
        bar01 = load_bar(code, "2026-09-01")
        bar829 = load_bar(code, "2026-08-29")
        if bar01:
            vsh = pct(o["price"], bar01["high"])
            vsc = pct(o["price"], bar01["close"])
            barb = "有"
        else:
            vsh = vsc = None
            barb = "无!!"
        vsp = pct(o["price"], bar829["close"]) if bar829 else None
        key = (code, o["price"], o["status"])
        if key in seen:
            continue
        seen.add(key)
        print(f"{code:8}{o['shares']:>5}{o['price']:>10.3f}{o['status']:>4}"
              f"{o['filled']:>5}" + fmt(vsh) + fmt(vsc) + fmt(vsp, 8) +
              f"  {barb:>8}")


if __name__ == "__main__":
    main()
