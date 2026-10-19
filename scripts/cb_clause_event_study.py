"""CB 下修条款事件研究 (2026-10-19, 预注册 logs/prereg_cb_clause_20261019.md).

H1=董事会提议下修公告; H2=预计触发下修提示公告.
入场=公告日后首个交易日 E 开盘; 出场=E+4 收盘(5日).
基准=CB等权同窗买入持有 mean_i(close_X/open_E-1)(v2, 修正隔夜口径);
v1 逐日开→收复利工件保留 logs/cb_clause_event_study_v1_daily_oc.json.
成本主口径0.1%双边.
统计=按券聚类t(主), 日聚类/naive t 并列. 判据出数前已锁.
用法: python scripts/cb_clause_event_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta

REGISTERED_AT = "2026-10-19T23:02:28+08:00"
START, END = "2022-01-01", "2026-09-30"
COST_PRIMARY = 0.001
COSTS = (0.0005, 0.001, 0.002)
DAILY = Path("data_store/convertible_bonds/daily")
EVENTS = {
    "H1": Path("data_store/meta/cb_clause_events_proposal.parquet"),
    "H2": Path("data_store/meta/cb_clause_events_trigger.parquet"),
}
MDE_PCT = {"H1": 0.220, "H2": 0.183}
N_MIN = {"H1": 200, "H2": 500}
B_MIN = {"H1": 100, "H2": 200}


def load_days():
    cal = load_meta("trade_calendar")
    days = sorted(pd.to_datetime(cal["trade_date"].astype(str).str[:10]).dt.normalize().unique())
    return np.array(days, dtype="datetime64[D]")


def shift_day(days, d, n):
    """n=1 → 严格晚于 d 的第 1 个交易日; n=5 → 入场的第 5 个交易日(含入场)."""
    i = np.searchsorted(days, np.datetime64(d, "D"), side="right")
    j = i + n - 1
    return pd.Timestamp(days[j]) if 0 <= j < len(days) else pd.NaT


def cluster_se(x, groups):
    x = np.asarray(x, dtype=float)
    groups = np.asarray(groups)
    n = len(x)
    if n < 2:
        return None
    mean = x.mean()
    s = pd.DataFrame({"r": x - mean, "g": groups}).groupby("g")["r"].sum()
    g = len(s)
    if g < 2:
        return None
    var = float((s ** 2).sum()) / n ** 2 * g / (g - 1)
    return float(np.sqrt(var)) if var > 0 else None


def stats(x, cost):
    x = x.dropna(subset=["ret5", "bench5"]).copy()
    if x.empty:
        return {"n": 0, "n_bonds": 0, "mean_excess_pct": None, "t_bond": None}
    x["excess"] = x["ret5"] - x["bench5"] - cost
    n, nb = len(x), x["code"].nunique()
    mean = float(x["excess"].mean())
    se_b = cluster_se(x["excess"].values, x["code"].values)
    se_d = cluster_se(x["excess"].values, x["ann_date"].astype(str).values)
    se_n = float(x["excess"].std(ddof=1) / np.sqrt(n)) if n > 1 else None
    daily = x.groupby("ann_date")["excess"].mean()
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else None
    return {
        "n": int(n), "n_bonds": int(nb),
        "mean_excess_pct": mean * 100,
        "median_pct": float(x["excess"].median() * 100),
        "win_rate_pct": float((x["excess"] > 0).mean() * 100),
        "t_naive": float(mean / se_n) if se_n and se_n > 0 else None,
        "t_day": float(mean / se_d) if se_d and se_d > 0 else None,
        "t_bond": float(mean / se_b) if se_b and se_b > 0 else None,
        "se_bond_pct": se_b * 100 if se_b else None,
        "mde_2se_pct": 2 * se_b * 100 if se_b else None,
        "max_dd_pct": dd * 100 if dd is not None else None,
    }


def build(kind, days, opens, closes):
    ev = pd.read_parquet(EVENTS[kind]).copy()
    ev["code"] = ev["code"].astype(str).str.zfill(6)
    ev["ann_date"] = pd.to_datetime(ev["ann_date"]).dt.normalize()
    ev = ev[(ev["ann_date"] >= START) & (ev["ann_date"] <= END)].copy()
    ev["entry"] = ev["ann_date"].map(lambda d: shift_day(days, d, 1))
    ev["exit5"] = ev["ann_date"].map(lambda d: shift_day(days, d, 5))
    ev["exit20"] = ev["ann_date"].map(lambda d: shift_day(days, d, 20))
    ev = ev.dropna(subset=["entry", "exit5"])

    invalid = {"missing_bar": 0, "dirty_50pct": 0, "limit_entry": 0, "limit_exit": 0,
               "missing_bench": 0, "missing_prev": 0}
    rows = []
    for code, g in ev.groupby("code"):
        p = DAILY / f"{code}.parquet"
        if not p.exists():
            invalid["missing_bar"] += len(g)
            continue
        d = pd.read_parquet(p)
        d["date"] = pd.to_datetime(d["date"]).dt.normalize()
        d = d.drop_duplicates("date").sort_values("date")
        for c in ("open", "close"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d[(d["open"] > 0) & (d["close"] > 0)]
        d["cc"] = d["close"].pct_change()
        d["prev_close"] = d["close"].shift(1)
        d = d.set_index("date")
        for _, r in g.iterrows():
            E, X5 = r["entry"], r["exit5"]
            if E not in d.index or X5 not in d.index:
                invalid["missing_bar"] += 1
                continue
            win = d.loc[E:X5]
            if (win["cc"].abs() > 0.5).any():
                invalid["dirty_50pct"] += 1
                continue
            pc_e, pc_x = d.loc[E, "prev_close"], d.loc[X5, "prev_close"]
            if pd.isna(pc_e) or pd.isna(pc_x):
                invalid["missing_prev"] += 1
                continue
            if d.loc[E, "open"] / pc_e - 1 >= 0.19:
                invalid["limit_entry"] += 1
                continue
            if d.loc[X5, "close"] / pc_x - 1 <= -0.19:
                invalid["limit_exit"] += 1
                continue
            # 基准: 同窗买入持有等权 mean_i(close_X_i/open_E_i - 1)
            if E not in opens.index or X5 not in closes.index:
                invalid["missing_bench"] += 1
                continue
            o_row, c_row = opens.loc[E], closes.loc[X5]
            ok = o_row.notna() & c_row.notna() & (o_row > 0) & (c_row > 0)
            if int(ok.sum()) < 50:
                invalid["missing_bench"] += 1
                continue
            bench5 = float((c_row[ok] / o_row[ok] - 1).mean())
            ret5 = float(d.loc[X5, "close"] / d.loc[E, "open"] - 1)

            # 诊断窗口 1 日/20 日(同口径)
            ret1 = float(d.loc[E, "close"] / d.loc[E, "open"] - 1)
            o1, c1 = opens.loc[E], closes.loc[E]
            ok1 = o1.notna() & c1.notna() & (o1 > 0) & (c1 > 0)
            b1 = float((c1[ok1] / o1[ok1] - 1).mean()) if int(ok1.sum()) >= 50 else np.nan
            ret20 = bench20 = None
            X20 = r.get("exit20")
            if pd.notna(X20) and X20 in d.index and X20 in closes.index:
                c20 = closes.loc[X20]
                ok20 = o_row.notna() & c20.notna() & (o_row > 0) & (c20 > 0)
                if int(ok20.sum()) >= 50:
                    bench20 = float((c20[ok20] / o_row[ok20] - 1).mean())
                    ret20 = float(d.loc[X20, "close"] / d.loc[E, "open"] - 1)
            rows.append({
                "code": code, "ann_date": r["ann_date"], "entry": E, "exit5": X5,
                "ret5": ret5, "bench5": bench5, "ret1": ret1, "bench1": b1,
                "ret20": ret20, "bench20": bench20,
            })
    out = pd.DataFrame(rows)
    return out, invalid, len(ev)


def verdict(res, oos, kind):
    return bool(
        res["mean_excess_pct"] is not None and res["mean_excess_pct"] > 0
        and res["t_bond"] is not None and res["t_bond"] >= 2.5
        and oos["mean_excess_pct"] is not None and oos["mean_excess_pct"] > 0
        and oos["t_bond"] is not None and oos["t_bond"] >= 1.5
        and res["n"] >= N_MIN[kind] and res["n_bonds"] >= B_MIN[kind]
        and res["mean_excess_pct"] >= MDE_PCT[kind]
        and res["mean_excess_pct"] >= COST_PRIMARY * 100
    )


def main():
    days = load_days()
    opens = load_meta("cb_open_panel")
    closes = load_meta("cb_close_panel")
    for panel in (opens, closes):
        panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    opens = opens.set_index("date").sort_index()
    closes = closes.set_index("date").sort_index()

    out = {"registered_at": REGISTERED_AT, "sample": [START, END],
           "cost_primary": COST_PRIMARY, "mde_pct": MDE_PCT,
           "benchmark": "v2_buy_and_hold: mean_i(close_X/open_E - 1)",
           "v1_artifact": "logs/cb_clause_event_study_v1_daily_oc.json",
           "results": {}}
    all_events = []
    for kind in ("H1", "H2"):
        x, invalid, n_raw = build(kind, days, opens, closes)
        all_events.append(x.assign(kind=kind))
        x["year"] = x["ann_date"].dt.year
        x["seg"] = np.where(x["year"] <= 2024, "IS_2022_2024", "OOS_2025_2026")
        res = {c: stats(x, c) for c in COSTS}
        primary = res[COST_PRIMARY]
        seg = {s: stats(x[x["seg"] == s], COST_PRIMARY) for s in ("IS_2022_2024", "OOS_2025_2026")}
        # 诊断窗口(仅主成本, 不入判据)
        diag = {}
        for w in ("ret1", "ret20"):
            bcol = {"ret1": "bench1", "ret20": "bench20"}[w]
            z = x.dropna(subset=[w, bcol]).copy()
            if not z.empty:
                z["excess"] = z[w] - z[bcol] - COST_PRIMARY
                se = cluster_se(z["excess"].values, z["code"].values)
                diag[w] = {"n": len(z), "mean_pct": float(z["excess"].mean() * 100),
                           "t_bond": float(z["excess"].mean() / se) if se else None}
        v = verdict(primary, seg["OOS_2025_2026"], kind)
        out["results"][kind] = {
            "n_raw": n_raw, "invalid": invalid,
            "primary": primary, "cost_sensitivity": {str(c): res[c] for c in COSTS},
            "segments": seg, "diagnostics": diag,
            "pass": v,
            "by_year": {int(k): int(v) for k, v in x.groupby("year").size().items()},
        }
        print(f"\n== {kind} == raw {n_raw} valid {primary['n']} bonds {primary['n_bonds']} "
              f"invalid {invalid}")
        print(f"  mean {primary['mean_excess_pct']:+.4f}%  median {primary['median_pct']:+.4f}%  "
              f"win {primary['win_rate_pct']:.1f}%  t_naive {primary['t_naive']:+.2f}  "
              f"t_day {primary['t_day']:+.2f}  t_bond {primary['t_bond']:+.2f}  "
              f"MDE {primary['mde_2se_pct']:.3f}%")
        for s, r in seg.items():
            print(f"  {s}: n={r['n']} mean {r['mean_excess_pct']:+.4f}% t_bond "
                  f"{r['t_bond'] if r['t_bond'] is None else round(r['t_bond'], 2)}")
        print(f"  cost 0.05/0.1/0.2% mean: "
              f"{res[0.0005]['mean_excess_pct']:+.4f} / {res[0.001]['mean_excess_pct']:+.4f} / "
              f"{res[0.002]['mean_excess_pct']:+.4f}")
        print(f"  diagnostics: {diag}")
        print(f"  PASS={v}")

    h1 = out["results"]["H1"]["pass"]
    h2 = out["results"]["H2"]["pass"]
    if h1:
        status = "H1候选(需独立组合/前向验证)"
    elif h2:
        status = "仅H2触发提示候选(H1不过)"
    else:
        status = "条款事件族关闭"
    out["verdict"] = {"H1_pass": h1, "H2_pass": h2, "status": status,
                      "multiple_testing": "下修相关累计N=3; H1/H2共用OOS 2025-2026"}
    print(f"\n判定: H1={h1} H2={h2} → {status}")

    Path("logs/cb_clause_event_study.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.concat(all_events, ignore_index=True).to_csv(
        "logs/cb_clause_events_returns.csv", index=False, encoding="utf-8-sig")
    print("\n已保存 logs/cb_clause_event_study.json 与 logs/cb_clause_events_returns.csv")


if __name__ == "__main__":
    main()
