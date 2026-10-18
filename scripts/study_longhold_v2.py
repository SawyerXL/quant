"""H2-v2: 长周期特征扩展(2012-2021 十 cohort), 判据见
logs/study_registration_longhold_v2.md(先于数据下载登记)。

数据: /data/quant_data_store/baostock_long/daily/{code}.parquet (2011-2025)。
输出: logs/longhold_panel_v2.csv + logs/study_longhold_v2_result.json
运行: timeout 1800 nice -n 10 ionice -c2 -n7 python3 scripts/study_longhold_v2.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

BS = Path("/data/quant_data_store/baostock_long/daily")
GLOBAL_END = pd.Timestamp("2025-12-31")
PANEL = Path("/root/quant/logs/longhold_panel_v2.csv")
RESULT = Path("/root/quant/logs/study_longhold_v2_result.json")
FORMATIONS = ["2012-12-31", "2013-12-31", "2014-12-31", "2015-12-31",
              "2016-12-31", "2017-12-29", "2018-12-28", "2019-12-31",
              "2020-12-31", "2021-12-31"]
PRIMARIES = ["pb", "mom12", "vol12", "amt60"]


def nearest_bar(d, target, max_gap=10):
    sub = d[d["date"] <= target]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    if (target - row["date"]).days > max_gap:
        return None, None
    return row, row["date"]


def build_panel():
    rows = []
    files = sorted(BS.glob("*.parquet"))
    skipped = 0
    t0 = datetime.now()
    for i, p in enumerate(files):
        if i % 1000 == 0:
            print(f"  build {i}/{len(files)} {datetime.now() - t0}", flush=True)
        try:
            d = pd.read_parquet(p)
        except Exception:
            skipped += 1
            continue
        if d.empty or "close" not in d.columns:
            skipped += 1
            continue
        d = d.copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date").reset_index(drop=True)
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d = d[d["close"] > 0].reset_index(drop=True)
        if len(d) < 300:
            continue
        code = str(d["code"].iloc[0]).split(".")[-1].zfill(6)
        first_date, last_date = d["date"].iloc[0], d["date"].iloc[-1]
        ret = d["close"].pct_change()
        for fs in FORMATIONS:
            ft = pd.Timestamp(fs)
            if (ft - first_date).days < 365:
                continue
            fbar, fdate = nearest_bar(d, ft)
            if fbar is None:
                continue
            pos = int(d.index[d["date"] == fdate][0])
            if pos < 252:
                continue
            win = d.iloc[pos - 251:pos + 1]
            c0 = float(fbar["close"])
            pb = pd.to_numeric(pd.Series([fbar.get("pbMRQ")]), errors="coerce").iloc[0]
            amt60 = pd.to_numeric(win["amount"].tail(60), errors="coerce").mean()
            turn60 = pd.to_numeric(win["turn"].tail(60), errors="coerce").mean()
            mom12 = c0 / float(win["close"].iloc[0]) - 1
            vol12 = float(ret.iloc[pos - 250:pos + 1].std())
            dd12 = float((win["close"] / win["close"].cummax() - 1).min())
            is_st = int(str(fbar.get("isST", "0")).lower() in ("1", "true"))
            rec = {"cohort": fs, "code": code, "pb": pb, "mom12": mom12,
                   "vol12": vol12, "amt60": amt60, "turn60": turn60,
                   "dd12": dd12, "is_st": is_st, "close0": c0}
            for years, col in ((3, "fwd3y"), (5, "fwd5y"), (10, "fwd10y")):
                tgt = fdate + pd.Timedelta(days=365 * years)
                bar, actual = nearest_bar(d, tgt, max_gap=15)
                if bar is not None:
                    rec[col] = float(bar["close"]) / c0 - 1
                    rec[f"{col}_delisted"] = 0
                elif tgt > GLOBAL_END + pd.Timedelta(days=15):
                    # 目标日在数据末端之后=未到期, 不得误标退市(H2-v1 隐患)
                    rec[col] = np.nan
                    rec[f"{col}_delisted"] = 0
                elif last_date < tgt - pd.Timedelta(days=15):
                    rec[col] = float(d["close"].iloc[-1]) / c0 - 1
                    rec[f"{col}_delisted"] = 1
                else:
                    rec[col] = np.nan
                    rec[f"{col}_delisted"] = 0
            rows.append(rec)
    panel = pd.DataFrame(rows)
    panel.to_csv(PANEL, index=False)
    print(f"面板: {len(panel)} 行 | 跳过文件 {skipped} | {datetime.now() - t0}")
    return panel


def quintile_stats(g, feat, target="fwd3y"):
    g = g.dropna(subset=[feat, target])
    if len(g) < 500:
        return None
    try:
        q = pd.qcut(g[feat], 5, labels=False, duplicates="drop")
    except ValueError:
        return None
    q1 = g[q == 0][target]
    q5 = g[q == q.max()][target]
    if len(q1) < 10 or len(q5) < 10:
        return None
    t = (q5.mean() - q1.mean()) / np.sqrt(
        q5.var(ddof=1) / len(q5) + q1.var(ddof=1) / len(q1))
    return {"n": int(len(g)), "q1": round(float(q1.mean()), 4),
            "q5": round(float(q5.mean()), 4),
            "spread": round(float(q5.mean() - q1.mean()), 4),
            "t": round(float(t), 2)}


def evaluate(panel, st_mode, target="fwd3y", min_cohorts=5,
             mean_thresh=0.05, t_need_ratio=0.6):
    out = {}
    for feat in PRIMARIES:
        res = {}
        for fs, g0 in panel.groupby("cohort"):
            g = g0 if st_mode == "incl_st" else g0[g0["is_st"] == 0]
            s = quintile_stats(g, feat, target=target)
            if s:
                res[fs] = s
        spreads = [v["spread"] for v in res.values()]
        ts = [abs(v["t"]) for v in res.values()]
        n_valid = len(res)
        same_dir = (n_valid >= 1 and
                    (all(s > 0 for s in spreads) or all(s < 0 for s in spreads)))
        mean_spread = float(np.mean(spreads)) if spreads else None
        need_t = int(np.ceil(t_need_ratio * n_valid)) if n_valid else 99
        strong = sum(1 for t in ts if t > 2.5)
        verdict = ("有证据" if (n_valid >= min_cohorts and same_dir
                              and mean_spread is not None
                              and abs(mean_spread) >= mean_thresh
                              and strong >= need_t)
                   else "无证据/不稳定")
        out[feat] = {"by_cohort": res, "n_valid_cohorts": n_valid,
                     "same_dir": same_dir,
                     "mean_spread": round(mean_spread, 4) if mean_spread is not None else None,
                     "t_gt_2.5": strong, "need_t": need_t, "verdict": verdict}
    return out


def main():
    panel = build_panel()
    print(panel.groupby("cohort").agg(n=("code", "size"),
                                      delisted3y=("fwd3y_delisted", "sum"),
                                      st=("is_st", "sum")).to_string())
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "registration_v2": "logs/study_registration_longhold_v2.md",
           "registration_h3": "logs/study_registration_longhold_h3.md",
           "h2v2_fwd3y_excl_st": evaluate(panel, "excl_st"),
           "h2v2_fwd3y_incl_st": evaluate(panel, "incl_st")}
    # H2-v2 退市股敏感性(剔退市重跑)
    surv = panel[panel["fwd3y_delisted"] == 0]
    out["h2v2_survivors_only"] = evaluate(surv, "excl_st")
    # H2-v2 子期
    early = panel[panel["cohort"] <= "2016-12-31"]
    late = panel[panel["cohort"] >= "2017-12-29"]
    out["h2v2_subperiod_2012_2016"] = evaluate(early, "excl_st")
    out["h2v2_subperiod_2017_2021"] = evaluate(late, "excl_st")
    # H2-v2 退市组
    dg = panel[panel["fwd3y_delisted"] == 1]
    out["h2v2_delisted"] = {"n": int(len(dg)),
                            "mean_fwd3y": round(float(dg["fwd3y"].mean()), 4) if len(dg) else None,
                            "survivor_mean": round(float(panel[panel["fwd3y_delisted"] == 0]["fwd3y"].mean()), 4)}
    # H2-v2 5年描述
    g5 = panel.dropna(subset=["fwd5y"])
    out["fwd5y_descriptive"] = {"n": int(len(g5)),
                                "mean": round(float(g5["fwd5y"].mean()), 4),
                                "median": round(float(g5["fwd5y"].median()), 4),
                                "positive_share": round(float((g5["fwd5y"] > 0).mean()), 4)}

    # ── H3: 十年期(2012-2015 形成) ─────────────────────────────
    h3 = panel[panel["cohort"] <= "2015-12-31"]
    out["h3_fwd10y_excl_st"] = evaluate(h3, "excl_st", target="fwd10y",
                                        min_cohorts=4, mean_thresh=0.10,
                                        t_need_ratio=0.75)
    out["h3_fwd10y_incl_st"] = evaluate(h3, "incl_st", target="fwd10y",
                                        min_cohorts=4, mean_thresh=0.10,
                                        t_need_ratio=0.75)
    # 十年期描述: 最高/最低十分位的形成日特征画像(明确不可用于选股)
    profile = {}
    for fs, g in h3.groupby("cohort"):
        g = g.dropna(subset=["fwd10y"])
        if len(g) < 500:
            continue
        top = g[g["fwd10y"] >= g["fwd10y"].quantile(0.9)]
        bot = g[g["fwd10y"] <= g["fwd10y"].quantile(0.1)]
        profile[fs] = {
            "n": int(len(g)),
            "top_decile": {k: round(float(top[k].mean()), 4) for k in
                           ("pb", "mom12", "vol12", "amt60", "is_st")},
            "bottom_decile": {k: round(float(bot[k].mean()), 4) for k in
                              ("pb", "mom12", "vol12", "amt60", "is_st")},
            "top10_mean_fwd10y": round(float(top["fwd10y"].mean()), 4),
            "bottom10_mean_fwd10y": round(float(bot["fwd10y"].mean()), 4),
        }
    out["h3_descriptive_deciles"] = profile
    RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print("\n===== H2-v2 三年期: 剔除 ST =====")
    for feat, v in out["h2v2_fwd3y_excl_st"].items():
        print(f"[{feat}] {v['verdict']} | cohorts={v['n_valid_cohorts']} "
              f"same_dir={v['same_dir']} mean_spread={v['mean_spread']} "
              f"t>2.5: {v['t_gt_2.5']}/{v['need_t']}")
        for fs, s in v["by_cohort"].items():
            print(f"   {fs}: n={s['n']} spread={s['spread']:+.1%} t={s['t']:+.1f}")
    print("\n===== H3 十年期(2012-2015): 剔除 ST =====")
    for feat, v in out["h3_fwd10y_excl_st"].items():
        print(f"[{feat}] {v['verdict']} | cohorts={v['n_valid_cohorts']} "
              f"same_dir={v['same_dir']} mean_spread={v['mean_spread']} "
              f"t>2.5: {v['t_gt_2.5']}/{v['need_t']}")
        for fs, s in v["by_cohort"].items():
            print(f"   {fs}: n={s['n']} Q1={s['q1']:+.1%} Q5={s['q5']:+.1%} "
                  f"spread={s['spread']:+.1%} t={s['t']:+.1f}")
    print("\nH3 十分位画像(描述性, 不可用于选股):")
    for fs, v in profile.items():
        print(f"   {fs}: top10 pb={v['top_decile']['pb']} mom={v['top_decile']['mom12']:+.1%} "
              f"vol={v['top_decile']['vol12']:.2f} amt={v['top_decile']['amt60']:.0f} | "
              f"bottom10 pb={v['bottom_decile']['pb']} mom={v['bottom_decile']['mom12']:+.1%} "
              f"vol={v['bottom_decile']['vol12']:.2f} amt={v['bottom_decile']['amt60']:.0f}")
    print("\n退市组:", out["h2v2_delisted"])
    print("5年描述:", out["fwd5y_descriptive"])
    print(f"\n结果已保存: {RESULT}")


if __name__ == "__main__":
    main()
