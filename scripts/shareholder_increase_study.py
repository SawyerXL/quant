"""重要股东增持 PEAD 事件研究(2026-10-20, 预注册 prereg_shareholder_increase_20261020.md)。

- 主事件: 重要股东(公司/集团/控股/投资类名称)增持, 占总股本>=0.10%;
  同一股票 20 交易日去重取首条。
- 入场: 公告日之后首个交易日开盘; 出场: 入场后第 20 个交易日收盘。
- 基准: 同窗等权买入持有; 成本 0.33%; 日聚类 t; IS/OOS; 60 日诊断。
用法: python scripts/shareholder_increase_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-10-19"
COSTS = (0.0033, 0.0023, 0.0053)
PRIMARY_COST = 0.0033
META = Path("/root/quant/data_store/meta")


def band_of(code):
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("688", "300", "301")):
        return 0.20
    return 0.10


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
    prices, opens = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy(); d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            op = pd.to_numeric(d["open"], errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl; opens[code] = op.reindex(cl.index)
        except Exception:
            continue
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(opens).sort_index()


def stats(x, cost):
    if x is None or x.empty:
        return {"n": 0}
    x = x.copy()
    x["ex"] = x["ret"] - x["bench"] - cost
    daily = x.groupby("entry")["ex"].mean()
    stock = x.groupby("code")["ex"].mean()
    n, nd, ns = len(x), len(daily), len(stock)
    td = float(daily.mean() / (daily.std() / np.sqrt(nd))) if nd > 1 and daily.std() > 0 else None
    ts = float(stock.mean() / (stock.std() / np.sqrt(ns))) if ns > 1 and stock.std() > 0 else None
    return {"n": int(n), "n_days": int(nd), "n_codes": int(ns),
            "mean_pct": float(x["ex"].mean() * 100),
            "median_pct": float(x["ex"].median() * 100),
            "win_pct": float((x["ex"] > 0).mean() * 100),
            "sd_pct": float(x["ex"].std() * 100),
            "daily_sd_pct": float(daily.std() * 100),
            "t_day": td, "t_stock": ts}


def main():
    sc = pd.read_parquet(META / "shareholder_changes.parquet")
    sc["公告日"] = pd.to_datetime(sc["公告日"])
    sc["code"] = sc["代码"].astype(str).str.zfill(6)
    sc["ratio"] = pd.to_numeric(sc["持股变动信息-占总股本比例"], errors="coerce")
    sc = sc[(sc["持股变动信息-增减"] == "增持") &
            (sc["公告日"] >= START) & (sc["公告日"] <= "2026-09-30")].copy()
    sc["corp"] = sc["股东名称"].map(is_corporate)
    print(f"增持 raw {len(sc)}, corp {int(sc.corp.sum())}")
    # 同 (code, 公告日) 取最大比例
    sc = sc.sort_values("ratio", ascending=False).drop_duplicates(["code", "公告日"])
    # 主/次
    prim = sc[sc.corp & (sc["ratio"] >= 0.10)].copy()
    sec = sc.copy()
    print(f"primary corp>=0.10% {len(prim)}, secondary all {len(sec)}")

    panel, opens = load_panel()
    pidx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(panel.index)}
    inv = [d.strftime("%Y-%m-%d") for d in panel.index]
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只")
    cl = panel.replace(0, np.nan)
    op = opens.reindex(cl.index).replace(0, np.nan)
    prev = cl.shift(1)
    band = pd.Series({c: band_of(c) for c in cl.columns})
    lim_up = prev.mul(1 + band, axis=1).round(2)
    lim_dn = prev.mul(1 - band, axis=1).round(2)
    ret = cl.pct_change()
    jump20 = ret.abs().rolling(20).max().shift(-19)
    bench20 = (cl.shift(-19) / op - 1).mean(axis=1)
    bench60 = (cl.shift(-59) / op - 1).mean(axis=1)
    jump60 = ret.abs().rolling(60).max().shift(-59)

    def dedupe_20(df):
        kept = []
        for _, g in df.sort_values("公告日").groupby("code"):
            last_i = None
            for r in g.to_dict("records"):
                A = pd.Timestamp(r["公告日"])
                j = int(cl.index.searchsorted(A, side="left"))
                if j >= len(cl.index):
                    continue
                if last_i is None or j - last_i >= 20:
                    kept.append(r); last_i = j
        return pd.DataFrame(kept)

    def build(df, hold):
        counts = {"events": 0, "no_entry": 0, "no_exit": 0, "limit_up": 0,
                  "limit_dn": 0, "jump": 0, "used": 0}
        rows = []
        b = bench20 if hold == 20 else bench60
        jm = jump20 if hold == 20 else jump60
        for r in df.to_dict("records"):
            code = r["code"]; A = pd.Timestamp(r["公告日"])
            if code not in cl.columns:
                counts["no_entry"] += 1; continue
            j = int(cl.index.searchsorted(A, side="left"))
            if j >= len(cl.index):
                counts["no_entry"] += 1; continue
            E_i = j + 1; X_i = E_i + hold - 1
            if X_i >= len(cl.index):
                counts["no_exit"] += 1; continue
            E = cl.index[E_i]; X = cl.index[X_i]
            pE = op.at[E, code]; pX = cl.at[X, code]
            if pd.isna(pE) or pd.isna(pX) or pE <= 0 or pX <= 0:
                counts["no_entry"] += 1; continue
            lu = lim_up.at[E, code]; ld = lim_dn.at[X, code]
            if lu == lu and pE >= lu - 1e-6:
                counts["limit_up"] += 1; continue
            if ld == ld and pX <= ld + 1e-6:
                counts["limit_dn"] += 1; continue
            jv = jm.at[E, code]
            if jv == jv and jv > 0.5:
                counts["jump"] += 1; continue
            bv = b.at[E]
            if bv != bv:
                counts["no_exit"] += 1; continue
            counts["events"] += 1
            rows.append({"code": code, "A": A.strftime("%Y-%m-%d"),
                         "entry": E.strftime("%Y-%m-%d"), "exit": X.strftime("%Y-%m-%d"),
                         "ret": pX / pE - 1, "bench": float(bv),
                         "ratio": r.get("ratio")})
            counts["used"] += 1
        return pd.DataFrame(rows), counts

    out = {"prereg": "logs/prereg_shareholder_increase_20261020.md",
           "raw": int(len(sc)), "primary_raw": int(len(prim)),
           "secondary_raw": int(len(sec)), "stats": {}}
    for name, df in (("primary", dedupe_20(prim)), ("secondary", dedupe_20(sec))):
        res20, c20 = build(df, 20)
        res60, c60 = build(df, 60)
        res20["year"] = pd.to_datetime(res20["entry"]).dt.year
        res20["seg"] = np.where(res20["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
        out[name] = {"counts20": c20, "counts60": c60, "n_dedup": int(len(df))}
        out["stats"][name] = {}
        for label, sub in (("all", res20), ("IS", res20[res20.seg == "IS_2019_2022"]),
                           ("OOS", res20[res20.seg == "OOS_2023_2026"])):
            out["stats"][name][label] = {str(c): stats(sub, c) for c in COSTS}
        out["stats"][name]["d60"] = stats(res60, PRIMARY_COST)
        res20.to_csv(f"/root/quant/logs/shareholder_{name}_returns.csv", index=False)
        print(f"{name}: counts20 {c20}")
        for label in ("all", "IS", "OOS"):
            s = out["stats"][name][label][str(PRIMARY_COST)]
            print(f"  {label}: n={s.get('n')} days={s.get('n_days')} mean={s.get('mean_pct')}% "
                  f"median={s.get('median_pct')}% win={s.get('win_pct')}% "
                  f"t_day={s.get('t_day')} t_stock={s.get('t_stock')}")
        print(f"  60d: {out['stats'][name]['d60']}")
    p = out["stats"]["primary"]["all"][str(PRIMARY_COST)]
    o = out["stats"]["primary"]["OOS"][str(PRIMARY_COST)]
    mde = 2.8 * p["daily_sd_pct"] / np.sqrt(max(1, p.get("n_days", 1))) if p.get("n") else None
    out["mde_pct"] = mde
    crit = {
        "1_mean_t": bool(p.get("mean_pct", -99) > 0 and p.get("t_day") is not None and p["t_day"] >= 2.5),
        "2_oos": bool(o.get("mean_pct", -99) > 0 and o.get("t_day") is not None and o["t_day"] >= 1.5),
        "3_n": bool(p.get("n", 0) >= 500 and p.get("n_days", 0) >= 200),
        "4_mde": bool(p.get("mean_pct", -99) >= max(mde or 99, 0.33)),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    print("MDE:", round(mde, 2) if mde else None, "criteria", crit, "verdict", out["verdict"])
    with open("/root/quant/logs/shareholder_increase_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
