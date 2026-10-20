"""大宗事件回避过滤 × pool30 重叠审计(2026-10-20, #21 后续; 只算重叠不算收益)。

why: 事件层负漂移 PASS 不等于组合层有增益——先看 pool30 的买入槽位里有多少
会被"大宗事件后 5 日回避"过滤掉。重叠太少 -> overlay 不值得跑(省一次检验)。
口径: 近似复刻引擎选池(前 20 日均额 TOP60 -> vol20<=5% -> 前 30), 统计每个
调仓日这 30 只里有多少只在过去 5 个交易日内有"折溢率<=-10% 且 额/流通>=0.5%"
的大宗事件(同 prereg 定义, 同股 5 日去重)。
用法: python scripts/block_filter_overlap_audit.py
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
    # 引擎 max_vol20=5.0 的口径是"日收益 std×100"(不是年化)——与
    # apply_filters 内 `win.std()*100` 对齐。
    vol20 = ret.rolling(20).std() * 100

    b = pd.read_parquet(META / "block_trade_events_2019_2026.parquet")
    b["交易日期"] = pd.to_datetime(b["交易日期"])
    b = b[(b["交易日期"] >= START) & (b["交易日期"] <= END)]
    b = b[(b["折溢率"] <= -0.10) & (b["成交总额/流通市值"] >= 0.005)].copy()
    b["code"] = b["证券代码"].astype(str).str.zfill(6)
    b = b.sort_values(["code", "交易日期"])
    # 同股 5 个交易日内去重(与事件研究一致)
    kept = []
    for _, g in b.groupby("code"):
        last = None
        for r in g.to_dict("records"):
            ds = pd.Timestamp(r["交易日期"]).strftime("%Y-%m-%d")
            i = pidx.get(ds)
            if i is None:
                continue
            if last is None or i - last >= 5:
                kept.append((r["code"], i)); last = i
    ev = pd.DataFrame(kept, columns=["code", "i"])
    by_code = {c: set(g["i"]) for c, g in ev.groupby("code")}
    print(f"大宗事件(过滤+去重) {len(ev)} 条, {ev.code.nunique()} 只")

    rows = []
    for ds in rebal:
        i = pidx.get(ds)
        if i is None or i < 25:
            continue
        amt_avg = ap.iloc[max(0, i - 20):i].mean().dropna()
        pool = amt_avg.nlargest(60).index.tolist()
        v = vol20.iloc[i - 1] if i - 1 < len(vol20) else None
        if v is not None:
            pool = [c for c in pool if c in v.index and (pd.isna(v[c]) or v[c] <= 5.0)]
        sel = pool[:30]
        # 过去 5 个交易日内有事件(i-5..i-1)
        excl = [c for c in sel if any((i - k) in by_code.get(c, set()) for k in range(1, 6))]
        rows.append({"date": ds, "n_sel": len(sel), "n_excl": len(excl),
                     "excl": ",".join(excl)})
    r = pd.DataFrame(rows)
    print(r[["n_sel", "n_excl"]].describe().to_string())
    print(f"总买入槽 {int(r.n_sel.sum())}; 被过滤 {int(r.n_excl.sum())} "
          f"({r.n_excl.sum()/max(1,r.n_sel.sum())*100:.2f}%)")
    print(f"有过滤的调仓日 {int((r.n_excl>0).sum())}/{len(r)}")
    print(r[r.n_excl > 0].tail(15).to_string(index=False))
    r.to_csv("/root/quant/logs/block_filter_overlap_20261020.csv", index=False)


if __name__ == "__main__":
    main()
