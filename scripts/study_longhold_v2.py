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
            for years, col in ((3, "fwd3y"), (5, "fwd5y")):
                tgt = fdate + pd.Timedelta(days=365 * years)
                bar, actual = nearest_bar(d, tgt, max_gap=15)
                if bar is not None:
                    rec[col] = float(bar["close"]) / c0 - 1
                    rec[f"{col}_delisted"] = 0
                else:
                    rec[col] = float(d["close"].iloc[-1]) / c0 - 1
                    rec[f"{col}_delisted"] = 1
            rows.append(rec)
    panel = pd.DataFrame(rows)
    panel.to_csv(PANEL, index=False)
    print(f"面板: {len(panel)} 行 | 跳过文件 {skipped} | {datetime.now() - t0}")
    return panel


def quintile_stats(g, feat):
    g = g.dropna(subset=[feat, "fwd3y"])
    if len(g) < 500:
        return None
    try:
        q = pd.qcut(g[feat], 5, labels=False, duplicates="drop")
    except ValueError:
        return None
    q1 = g[q == 0]["fwd3y"]
    q5 = g[q == q.max()]["fwd3y"]
    if len(q1) < 10 or len(q5) < 10:
        return None
    t = (q5.mean() - q1.mean()) / np.sqrt(
        q5.var(ddof=1) / len(q5) + q1.var(ddof=1) / len(q1))
    return {"n": int(len(g)), "q1": round(float(q1.mean()), 4),
            "q5": round(float(q5.mean()), 4),
            "spread": round(float(q5.mean() - q1.mean()), 4),
            "t": round(float(t), 2)}


def evaluate(panel, st_mode):
    out = {}
    for feat in PRIMARIES:
        res = {}
        for fs, g0 in panel.groupby("cohort"):
            g = g0 if st_mode == "incl_st" else g0[g0["is_st"] == 0]
            s = quintile_stats(g, feat)
            if s:
                res[fs] = s
        spreads = [v["spread"] for v in res.values()]
        ts = [abs(v["t"]) for v in res.values()]
        n_valid = len(res)
        same_dir = (n_valid >= 1 and
                    (all(s > 0 for s in spreads) or all(s < 0 for s in spreads)))
        mean_spread = float(np.mean(spreads)) if spreads else None
        need_t = int(np.ceil(0.6 * n_valid)) if n_valid else 99
        strong = sum(1 for t in ts if t > 2.5)
        verdict = ("有证据" if (n_valid >= 5 and same_dir and mean_spread is not None
                              and abs(mean_spread) >= 0.05 and strong >= need_t)
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
           "registration": "logs/study_registration_longhold_v2.md",
           "excl_st": evaluate(panel, "excl_st"),
           "incl_st": evaluate(panel, "incl_st")}
    # 退市股敏感性(剔退市重跑)
    surv = panel[panel["fwd3y_delisted"] == 0]
    out["survivors_only"] = evaluate(surv, "excl_st")
    # 子期
    early = panel[panel["cohort"] <= "2016-12-31"]
    late = panel[panel["cohort"] >= "2017-12-29"]
    out["subperiod_2012_2016"] = evaluate(early, "excl_st")
    out["subperiod_2017_2021"] = evaluate(late, "excl_st")
    # 退市组
    dg = panel[panel["fwd3y_delisted"] == 1]
    out["delisted"] = {"n": int(len(dg)),
                       "mean_fwd3y": round(float(dg["fwd3y"].mean()), 4) if len(dg) else None,
                       "survivor_mean": round(float(panel[panel["fwd3y_delisted"] == 0]["fwd3y"].mean()), 4)}
    # 5年描述
    g5 = panel.dropna(subset=["fwd5y"])
    g5 = g5[g5["cohort"] <= "2020-12-31"]
    out["fwd5y_descriptive"] = {"n": int(len(g5)),
                                "mean": round(float(g5["fwd5y"].mean()), 4),
                                "median": round(float(g5["fwd5y"].median()), 4),
                                "positive_share": round(float((g5["fwd5y"] > 0).mean()), 4)}
    RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print("\n===== 主结果: 剔除 ST =====")
    for feat, v in out["excl_st"].items():
        print(f"[{feat}] {v['verdict']} | cohorts={v['n_valid_cohorts']} "
              f"same_dir={v['same_dir']} mean_spread={v['mean_spread']} "
              f"t>2.5: {v['t_gt_2.5']}/{v['need_t']}")
        for fs, s in v["by_cohort"].items():
            print(f"   {fs}: n={s['n']} spread={s['spread']:+.1%} t={s['t']:+.1f}")
    print("\n退市组:", out["delisted"])
    print("5年描述:", out["fwd5y_descriptive"])
    print(f"\n结果已保存: {RESULT}")


if __name__ == "__main__":
    main()
