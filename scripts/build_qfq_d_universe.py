"""D 方案宇宙（2026-10-21）：2019-2026 成交额 TOP 并集。

- rebal60/rebal120：双周调仓日前 20 日均额 TOP60/TOP120（引擎实际池口径）。
- daily300：日度成交额 TOP300 并集（研究常用池）。
输出 `logs/qfq_d_universe_20261021.csv`（code + 三个 tier 布尔）。
只读，不写库。
用法：python scripts/build_qfq_d_universe.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_engine import make_rebal_dates

START, END = "2019-01-01", "2026-10-19"
OUT = Path("logs/qfq_d_universe_20261021.csv")


def main() -> int:
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [pd.Timestamp(d) for d in make_rebal_dates(cal, "biweekly")
             if START <= d <= END]
    meta = load_meta("stock_info_full")
    codes = meta["code"].astype(str).str.zfill(6).tolist() if not meta.empty else []
    amts = {}
    for i, code in enumerate(codes):
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            a = pd.to_numeric(d.set_index(pd.to_datetime(d["date"]))["amount"],
                              errors="coerce").dropna()
            if len(a) >= 250:
                amts[code] = a
        except Exception:
            pass
        if i % 500 == 0:
            print("load", i, "ok", len(amts), flush=True)
    A = pd.DataFrame(amts).sort_index()
    print(f"amount panel {A.shape}", flush=True)
    r60, r120 = set(), set()
    for dt in rebal:
        if dt not in A.index:
            continue
        j = A.index.get_loc(dt)
        avg = A.iloc[max(0, j - 20):j].mean().dropna()
        if len(avg):
            r60 |= set(avg.nlargest(60).index)
            r120 |= set(avg.nlargest(120).index)
    daily = set()
    for j in range(len(A)):
        row = A.iloc[j].dropna()
        if len(row):
            daily |= set(row.nlargest(300).index)
    allc = sorted(r60 | r120 | daily)
    out = pd.DataFrame({"code": allc})
    out["rebal60"] = out["code"].isin(r60)
    out["rebal120"] = out["code"].isin(r120)
    out["daily300"] = out["code"].isin(daily)
    out.to_csv(OUT, index=False)
    print({"rebal60": len(r60), "rebal120": len(r120), "daily300": len(daily),
           "union": len(allc)}, flush=True)
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
