"""H2 长周期研究: A股事前特征 → 3/5 年前瞻收益(PIT宇宙含退市)。

口径与判据见 logs/study_registration_longhold.md(先于出数登记)。
输出 logs/longhold_panel.csv + logs/study_longhold_result.json。
运行: timeout 1200 nice -n 10 ionice -c2 -n7 python3 scripts/study_longhold.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

BS = Path("/data/quant_data_store/baostock/daily")
PANEL = Path("/root/quant/logs/longhold_panel.csv")
RESULT = Path("/root/quant/logs/study_longhold_result.json")
FORMATIONS = ["2019-12-31", "2020-12-31", "2021-12-31"]


def _nearest_bar(d: pd.DataFrame, target: pd.Timestamp, max_gap_days: int = 10):
    """目标日最近 bar(≤target 且相差≤max_gap_days), 返回 (row, actual_date)。"""
    sub = d[d["date"] <= target]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    if (target - row["date"]).days > max_gap_days:
        return None, None
    return row, row["date"]


def main():
    rows = []
    files = sorted(BS.glob("*.parquet"))
    skipped = 0
    t0 = datetime.now()
    for i, p in enumerate(files):
        if i % 1500 == 0:
            print(f"  {i}/{len(files)} {datetime.now() - t0}", flush=True)
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
        code = str(d["code"].iloc[0]).split(".")[0].zfill(6)
        first_date = d["date"].iloc[0]
        last_date = d["date"].iloc[-1]
        ret = d["close"].pct_change()
        for fs in FORMATIONS:
            ft = pd.Timestamp(fs)
            if (ft - first_date).days < 365:
                continue
            fbar, fdate = _nearest_bar(d, ft)
            if fbar is None:
                continue
            pos = int(d.index[d["date"] == fdate][0])
            if pos < 252:
                continue
            win = d.iloc[pos - 251:pos + 1]
            c0 = float(fbar["close"])
            # 特征
            pb = pd.to_numeric(pd.Series([fbar.get("pbMRQ")]), errors="coerce").iloc[0]
            amt60 = pd.to_numeric(win["amount"].tail(60), errors="coerce").mean() \
                if "amount" in win else np.nan
            turn60 = pd.to_numeric(win["turn"].tail(60), errors="coerce").mean() \
                if "turn" in win else np.nan
            mom12 = c0 / float(win["close"].iloc[0]) - 1
            vol12 = float(ret.iloc[pos - 250:pos + 1].std())
            roll_max = win["close"].cummax()
            dd12 = float((win["close"] / roll_max - 1).min())
            is_st = int(str(fbar.get("isST", "0")).lower() in ("1", "true"))
            rec = {"cohort": fs, "code": code, "pb": pb, "mom12": mom12,
                   "vol12": vol12, "amt60": amt60, "turn60": turn60,
                   "dd12": dd12, "is_st": is_st, "close0": c0,
                   "first_date": str(first_date.date())}
            # 前瞻 3/5 年
            for years, col in ((3, "fwd3y"), (5, "fwd5y")):
                tgt = fdate + pd.Timedelta(days=365 * years)
                bar, actual = _nearest_bar(d, tgt, max_gap_days=15)
                if bar is not None and (tgt - actual).days <= 15:
                    rec[col] = float(bar["close"]) / c0 - 1
                    rec[f"{col}_delisted"] = 0
                else:
                    rec[col] = float(d["close"].iloc[-1]) / c0 - 1
                    rec[f"{col}_delisted"] = 1
                    rec[f"{col}_last_date"] = str(last_date.date())
            rows.append(rec)

    panel = pd.DataFrame(rows)
    panel.to_csv(PANEL, index=False)
    print(f"面板: {len(panel)} 行 | 跳过文件 {skipped} | 耗时 {datetime.now() - t0}")
    print(panel.groupby("cohort").agg(
        n=("code", "size"), delisted3y=("fwd3y_delisted", "sum"),
        st=("is_st", "sum")).to_string())

    # ── 检验 ────────────────────────────────────────────────
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "n_files_skipped": skipped, "criteria": "注册文件 H2 §4", "features": {}}
    primaries = ["pb", "mom12", "vol12", "amt60"]
    for feat in primaries:
        res = {}
        for fs, g in panel.groupby("cohort"):
            g = g.dropna(subset=[feat, "fwd3y"])
            g = g[g["is_st"] == 0]
            if len(g) < 200:
                res[fs] = {"n": len(g), "skip": "样本不足"}
                continue
            try:
                q = pd.qcut(g[feat], 5, labels=False, duplicates="drop")
            except ValueError:
                res[fs] = {"n": len(g), "skip": "分组失败"}
                continue
            q1 = g[q == 0]["fwd3y"]
            q5 = g[q == q.max()]["fwd3y"]
            t = None
            if len(q1) > 2 and len(q5) > 2:
                t = float((q5.mean() - q1.mean()) / np.sqrt(
                    q5.var(ddof=1) / len(q5) + q1.var(ddof=1) / len(q1)))
            res[fs] = {"n": int(len(g)), "q1_mean": round(float(q1.mean()), 4),
                       "q5_mean": round(float(q5.mean()), 4),
                       "spread": round(float(q5.mean() - q1.mean()), 4),
                       "t": round(t, 2) if t is not None else None}
        spreads = [v.get("spread") for v in res.values() if "spread" in v]
        ts = [v.get("t") for v in res.values() if v.get("t") is not None]
        same_dir = (len(spreads) == 3 and
                    (all(s > 0 for s in spreads) or all(s < 0 for s in spreads)))
        mean_spread = float(np.mean(spreads)) if spreads else None
        strong = sum(1 for t in ts if abs(t) > 2.5)
        verdict = ("有证据" if (same_dir and mean_spread is not None
                              and abs(mean_spread) >= 0.05 and strong >= 2)
                   else "无证据/不稳定")
        out["features"][feat] = {"by_cohort": res, "same_dir": same_dir,
                                 "mean_spread": round(mean_spread, 4) if mean_spread is not None else None,
                                 "t_gt_2.5_cohorts": strong, "verdict": verdict}
        print(f"\n[{feat}] verdict={verdict} mean_spread={mean_spread:+.1%} "
              f"same_dir={same_dir} strong_t={strong}/3")
        for fs, v in res.items():
            print(f"   {fs}: {v}")

    # 退市组 vs 存活组(3年)
    del_g = panel[panel["fwd3y_delisted"] == 1]
    out["delisted_vs_survivor"] = {
        "n_delisted": int(len(del_g)),
        "delisted_mean_fwd3y": round(float(del_g["fwd3y"].mean()), 4) if len(del_g) else None,
        "survivor_mean_fwd3y": round(float(panel[panel["fwd3y_delisted"] == 0]["fwd3y"].mean()), 4),
    }
    # 5年(2019 cohort) 描述性
    g5 = panel[(panel["cohort"] == "2019-12-31")].dropna(subset=["fwd5y"])
    out["fwd5y_2019_descriptive"] = {
        "n": int(len(g5)), "mean": round(float(g5["fwd5y"].mean()), 4),
        "median": round(float(g5["fwd5y"].median()), 4),
        "share_positive": round(float((g5["fwd5y"] > 0).mean()), 4),
    }
    RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n结果已保存: {RESULT}")
    print(json.dumps(out.get("delisted_vs_survivor", {}), ensure_ascii=False))
    print(json.dumps(out.get("fwd5y_2019_descriptive", {}), ensure_ascii=False))


if __name__ == "__main__":
    main()
