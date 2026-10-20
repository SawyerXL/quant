"""重要股东减持回避 × pool30 重叠审计(2026-10-20, 只算重叠不算收益)。

why: 事件层负漂移 PASS 不等于组合层有增益——先看 pool30 的买入槽里有多少会被
"减持公告后 20 个交易日回避"过滤。重叠太少 -> overlay 不值得跑。
口径: 近似复刻引擎选池(前 20 日均额 TOP60 -> 日波动<5% -> 前 30), 统计每个调仓日
这 30 只里有多少只在过去 20 个交易日内有"重要股东(corp)减持且占比>=0.1%"事件。
用法: python scripts/shareholder_decrease_overlap_audit.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_engine import make_rebal_dates

START, END = "2019-01-01", "2026-10-19"
META = Path("/root/quant/data_store/meta")


def is_corporate(name):
    s = str(name)
    corp = ("有限公司", "集团", "控股", "投资", "合伙")
    prod = ("基金", "资产管理", "信托", "证券", "保险", "银行", "计划")
    return any(k in s for k in corp) and not any(k in s for k in prod)


def main():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    codes = sorted(c for c in meta["code"] if not st.get(c, False))
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy(); d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            continue
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index().reindex(panel.index)
    pidx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(panel.index)}
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只")

    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    ret = panel.pct_change()
    vol20 = ret.rolling(20).std() * 100

    sc = pd.read_parquet(META / "shareholder_changes.parquet")
    sc["公告日"] = pd.to_datetime(sc["公告日"])
    sc["code"] = sc["代码"].astype(str).str.zfill(6)
    sc["ratio"] = pd.to_numeric(sc["持股变动信息-占总股本比例"], errors="coerce")
    sc = sc[(sc["持股变动信息-增减"] == "减持") &
            (sc["公告日"] >= START) & (sc["公告日"] <= "2026-09-30")].copy()
    sc["corp"] = sc["股东名称"].map(is_corporate)
    sc = sc[sc.corp & (sc["ratio"] >= 0.10)]
    sc = sc.sort_values("ratio", ascending=False).drop_duplicates(["code", "公告日"])
    # 同股 20 交易日去重
    kept = []
    for _, g in sc.sort_values("公告日").groupby("code"):
        last = None
        for r in g.to_dict("records"):
            ds = pd.Timestamp(r["公告日"]).strftime("%Y-%m-%d")
            i = pidx.get(ds)
            if i is None:
                continue
            if last is None or i - last >= 20:
                kept.append((r["code"], i)); last = i
    ev = pd.DataFrame(kept, columns=["code", "i"])
    by_code = {c: set(g["i"]) for c, g in ev.groupby("code")}
    print(f"减持事件(去重) {len(ev)} 条, {ev.code.nunique()} 只")

    rows = []
    for ds in rebal:
        i = pidx.get(ds)
        if i is None or i < 25:
            continue
        amt_avg = ap.iloc[max(0, i - 20):i].mean().dropna()
        pool = amt_avg.nlargest(60).index.tolist()
        v = vol20.iloc[i - 1]
        pool = [c for c in pool if c in v.index and (pd.isna(v[c]) or v[c] <= 5.0)]
        sel = pool[:30]
        # 过去 20 个交易日内有减持事件(i-20..i-1)
        excl = [c for c in sel if any((i - k) in by_code.get(c, set()) for k in range(1, 21))]
        rows.append({"date": ds, "n_sel": len(sel), "n_excl": len(excl),
                     "excl": ",".join(excl)})
    r = pd.DataFrame(rows)
    print(r[["n_sel", "n_excl"]].describe().to_string())
    print(f"总买入槽 {int(r.n_sel.sum())}; 被过滤 {int(r.n_excl.sum())} "
          f"({r.n_excl.sum()/max(1,r.n_sel.sum())*100:.2f}%)")
    print(f"有过滤的调仓日 {int((r.n_excl>0).sum())}/{len(r)}")
    print(r[r.n_excl > 0].tail(15).to_string(index=False))
    r.to_csv("/root/quant/logs/shareholder_decrease_overlap_20261020.csv", index=False)


if __name__ == "__main__":
    main()
