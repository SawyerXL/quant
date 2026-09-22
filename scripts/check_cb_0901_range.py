"""09-01 深市6只CB委托价 vs 当日区间对照(废单调查第一步, 用户方案2026-09-22)
输出: 每只券的全部CB买入委托(含废单), 委托价与当日[low,high]的位置关系。
"""
import json
from pathlib import Path

import pandas as pd

CODES = ["123107", "127027", "127040", "127061", "128134", "128142"]
DAY = "2026-09-01"
POS_FILE = Path("logs/qmt_positions_20260901.json")
CB_DAILY = Path("data_store/convertible_bonds/daily")


def load_orders():
    d = json.loads(POS_FILE.read_text())
    orders = []
    for o in d.get("today_orders", []):
        code = o["code"].split(".")[0]
        if code in CODES:
            orders.append(o)
    return orders


def load_bar(code: str) -> dict | None:
    f = CB_DAILY / f"{code}.parquet"
    if not f.exists():
        print(f"  !! {code} 无日线文件 {f}")
        return None
    df = pd.read_parquet(f)
    date_col = "date" if "date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
    row = df[df[date_col] == DAY]
    if row.empty:
        print(f"  !! {code} 无 {DAY} bar (最后: {df[date_col].max()})")
        return None
    r = row.iloc[0]
    return {
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
    }


def main():
    orders = load_orders()
    print(f"=== 09-01 CB 订单共 {len(orders)} 笔 ===\n")
    # 每只券的日线区间
    bars = {c: load_bar(c) for c in CODES}

    rows = []
    for o in orders:
        code = o["code"].split(".")[0]
        bar = bars.get(code)
        price = float(o["price"])
        if bar is None:
            rows.append((code, o["direction"], o["shares"], price,
                         o["filled"], o["status"], None, None, None, None))
            continue
        # 委托价相对当日区间的位置
        above_high = (price / bar["high"] - 1) * 100
        in_range = bar["low"] <= price <= bar["high"]
        vs_close = (price / bar["close"] - 1) * 100
        rows.append((code, o["direction"], o["shares"], price, o["filled"],
                     o["status"], in_range, round(above_high, 2),
                     round(vs_close, 2), o["order_id"]))

    print(f"{'code':8} {'dir':4} {'张':>5} {'委托价':>10} {'成交':>5} "
          f"{'状态':>4} {'区间内':>4} {'vs当日高%':>9} {'vs收盘%':>8} {'order_id'}")
    for r in sorted(rows, key=lambda x: (x[0], x[1])):
        print(f"{r[0]:8} {r[1]:4} {r[2]:>5} {r[3]:>10.3f} {r[4]:>5} "
              f"{r[5]:>4} {str(r[6]):>6} {r[7]:>9} {r[8]:>8} {r[9]}")

    print("\n=== 当日区间 ===")
    for c in CODES:
        b = bars.get(c)
        if b:
            print(f"{c}: low={b['low']:.3f} high={b['high']:.3f} "
                  f"open={b['open']:.3f} close={b['close']:.3f}")

    # 状态码: 56=已成交 57=废单 50=已撤单/过期
    print("\n=== 按券汇总(成交 vs 废单/撤单) ===")
    from collections import Counter
    for c in CODES:
        cs = [o for o in orders if o["code"].split(".")[0] == c]
        stats = Counter((o["direction"], o["status"]) for o in cs)
        print(f"{c}: {dict(stats)}")


if __name__ == "__main__":
    main()
