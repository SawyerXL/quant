"""重要股东减持回避 × pool30 组合层 A/B(2026-10-20, N=60)。

预注册: logs/prereg_shareholder_decrease_overlay_20261020.md
- 事件: 重要股东(corp)减持且占比>=0.10%; 同股 20 交易日去重。
- 排除: 调仓日 R 前 1~20 个交易日内有事件的非持仓候选, 下一名补位; 已持仓不强制卖。
- 引擎: apply_filters(exclude,held)/run_backtest(exclude_map) 默认 None 行为不变。
- 主=50万/组 lot; 对照=无约束; 全期 2019-2026 + OOS 2015-2018。
用法: python scripts/backtest_decrease_filter_overlay.py
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2015-01-01", "2026-10-19"
FULL_START = "2019-01-01"
OOS = ("2015-01-01", "2018-12-31")
META = Path("/root/quant/data_store/meta")
BLOCK = (6, 2000, 20261020)


def is_corporate(name):
    s = str(name)
    corp = ("有限公司", "集团", "控股", "投资", "合伙")
    prod = ("基金", "资产管理", "信托", "证券", "保险", "银行", "计划")
    return any(k in s for k in corp) and not any(k in s for k in prod)


def load_panel():
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
    return panel, ap


def load_events(pidx):
    sc = pd.read_parquet(META / "shareholder_changes.parquet")
    sc["公告日"] = pd.to_datetime(sc["公告日"])
    sc["code"] = sc["代码"].astype(str).str.zfill(6)
    sc["ratio"] = pd.to_numeric(sc["持股变动信息-占总股本比例"], errors="coerce")
    sc = sc[(sc["持股变动信息-增减"] == "减持") &
            (sc["公告日"] >= START) & (sc["公告日"] <= "2026-09-30")].copy()
    sc["corp"] = sc["股东名称"].map(is_corporate)
    sc = sc[sc.corp & (sc["ratio"] >= 0.10)]
    sc = sc.sort_values("ratio", ascending=False).drop_duplicates(["code", "公告日"])
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
    return pd.DataFrame(kept, columns=["code", "i"])


def build_exclude(events, pidx_inv, rebal_set):
    ex = {}
    for code, i in events[["code", "i"]].itertuples(index=False, name=None):
        for k in range(1, 21):
            j = int(i) + k
            if j >= len(pidx_inv):
                break
            d = pidx_inv[j]
            if d in rebal_set:
                ex.setdefault(d, set()).add(code)
    return ex


def metric(nav):
    cm = calc_metrics(nav)
    return {"ann": float(str(cm["年化收益率"]).strip("%")),
            "sharpe": float(cm["夏普比率"]),
            "dd": float(str(cm["最大回撤"]).strip("%"))}


def monthly(nav):
    r = nav.pct_change().dropna()
    return (1 + r).resample("ME").prod() - 1


def block_boot(diff, block, B, seed):
    x = np.asarray(diff.dropna(), dtype=float)
    if len(x) < block + 1:
        return None, None
    rng = np.random.default_rng(seed)
    sm = len(x) - block
    means = []
    for _ in range(B):
        starts = rng.integers(0, sm + 1, size=int(np.ceil(len(x) / block)))
        s = np.concatenate([x[a:a + block] for a in starts])[:len(x)]
        means.append((1 + s.mean()) ** 12 - 1)
    return float(np.mean(means)), float(np.std(means))


def main():
    panel, ap = load_panel()
    pidx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(panel.index)}
    pidx_inv = [d.strftime("%Y-%m-%d") for d in panel.index]
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只")
    events = load_events(pidx)
    print(f"events {len(events)}, {events.code.nunique()} 只")
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    rebal_all = make_rebal_dates(cal, "biweekly")
    full_rebal = [d for d in rebal_all if FULL_START <= d <= END]
    oos_rebal = [d for d in rebal_all if OOS[0] <= d <= OOS[1]]
    ex_map = build_exclude(events, pidx_inv, set(full_rebal) | set(oos_rebal))
    print(f"exclude_map {len(ex_map)} 日, {sum(len(v) for v in ex_map.values())} 对")

    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)

    def slice_window(lo, hi):
        p = panel.loc[lo:hi]; a = ap.loc[lo:hi]
        cols = p.notna().sum() >= 250
        return p.loc[:, cols], a.loc[:, cols]

    full_panel, full_ap = slice_window(FULL_START, END)
    oos_panel, oos_ap = slice_window(OOS[0], OOS[1])
    configs = {
        "lot50": replace(DEFAULT_CONFIG, lot_size=100, initial_capital=500_000.0),
        "uncon": replace(DEFAULT_CONFIG, lot_size=0, initial_capital=500_000.0),
    }
    windows = {"full": (full_panel, full_ap, full_rebal),
               "oos": (oos_panel, oos_ap, oos_rebal)}
    out = {"prereg": "logs/prereg_shareholder_decrease_overlay_20261020.md",
           "as_of": END, "events": int(len(events)),
           "exclude_pairs": sum(len(v) for v in ex_map.values()),
           "exclude_dates": len(ex_map), "arms": {}}
    for wname, (pw, aw, rw) in windows.items():
        for cname, cfg in configs.items():
            nav_b, m_b = run_backtest(pw, aw, rw, cfg, idx_c)
            nav_f, m_f = run_backtest(pw, aw, rw, cfg, idx_c, exclude_map=ex_map)
            mb, mf = metric(nav_b), metric(nav_f)
            key = f"{wname}_{cname}"
            out["arms"][key] = {
                "baseline": {**mb, "lot_skips": int(m_b.get("lot_skips", 0)),
                             "trades": int(m_b.get("trades", 0))},
                "filtered": {**mf, "lot_skips": int(m_f.get("lot_skips", 0)),
                             "trades": int(m_f.get("trades", 0))},
                "diff": {"ann_pp": round(mf["ann"] - mb["ann"], 4),
                         "sharpe": round(mf["sharpe"] - mb["sharpe"], 4),
                         "dd_pp": round(mf["dd"] - mb["dd"], 4)}}
            if wname == "full":
                dif = monthly(nav_f) - monthly(nav_b)
                mean_ann, se_ann = block_boot(dif, *BLOCK)
                out["arms"][key]["bootstrap"] = {
                    "months": int(len(dif)), "mean_ann": mean_ann, "se_ann": se_ann,
                    "t": (mean_ann / se_ann) if se_ann else None}
            print(key, "base", {k: round(v, 3) for k, v in mb.items()},
                  "filt", {k: round(v, 3) for k, v in mf.items()},
                  "diff", out["arms"][key]["diff"], flush=True)
    with open("/root/quant/logs/shareholder_decrease_overlay_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("-> logs/shareholder_decrease_overlay_20261020.json")


if __name__ == "__main__":
    main()
