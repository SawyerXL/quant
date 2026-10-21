"""CB 双低排名分桶诊断（F 区挂账，2026-10-21）。

问题（F 区 09-27）：TOP25 的 25/26 边界很薄（间隔中位 0.11），
边界换人是否有害？TOP20/25/30/40 该取多少？
判据（用户裁定口径，非回测）：看排名分桶月收益
（第1-10/11-20/21-25/26-30/31-40 名）——
①若 26-30 与 21-25 无显著差别 → 边界换人无害 → TOP25 保持；
②若单调递减明显 → 排名有效 → 反而支持 TOP20。

口径：每月快照用 `data/cb_select.filter_universe`（与实盘共用），
按 dblow 升序分桶；月收益=下一快照日收盘/本快照日收盘−1（债券中途
退市用最后收盘，含退市价格；不做分红/兑付调整）。描述性诊断，非回测。
用法：python scripts/cb_top_bucket_diag.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_meta
from data.cb_select import filter_universe

OUT = Path("logs/cb_top_bucket_diag_20261021.json")
BUCKETS = [(0, 10, "1-10"), (10, 20, "11-20"), (20, 25, "21-25"),
           (25, 30, "26-30"), (30, 40, "31-40")]


def load_daily_closes() -> dict:
    out = {}
    for f in Path("data_store/convertible_bonds/daily").glob("*.parquet"):
        try:
            d = pd.read_parquet(f, columns=["date", "close"])
            d["date"] = pd.to_datetime(d["date"])
            s = pd.to_numeric(d.set_index("date")["close"],
                              errors="coerce").dropna().sort_index()
            if len(s):
                out[f.stem] = s
        except Exception:
            pass
    return out


def px_at(series: pd.Series, dt: pd.Timestamp):
    s = series[series.index <= dt]
    return float(s.iloc[-1]) if len(s) else None


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 10:
        return {"n": int(len(r))}
    mean = float(r.mean())
    t = mean / (r.std() / np.sqrt(len(r))) if r.std() > 0 else 0.0
    return {"n": int(len(r)), "mean_monthly_pct": round(mean * 100, 3),
            "ann_pct": round(((1 + mean) ** 12 - 1) * 100, 2),
            "t": round(float(t), 2),
            "win_pct": round(float((r > 0).mean()) * 100, 1)}


def main() -> int:
    snaps = load_meta("cb_snapshots")
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    dates = sorted(snaps["snap_date"].unique())
    print(f"snapshots {len(dates)} {dates[0].date()}~{dates[-1].date()}",
          flush=True)
    closes = load_daily_closes()
    print(f"daily files {len(closes)}", flush=True)

    buckets = {name: [] for _, _, name in BUCKETS}
    top20, top25 = [], []
    cover = []
    for i in range(len(dates) - 1):
        d0, d1 = dates[i], dates[i + 1]
        snap = snaps[snaps["snap_date"] == d0]
        filt = filter_universe(snap)
        filt = filt.dropna(subset=["dblow"]).sort_values("dblow")
        codes = filt["code"].astype(str).tolist()
        if len(codes) < 40:
            continue
        rets = {}
        for c in codes[:40]:
            s = closes.get(c)
            if s is None:
                continue
            p0, p1 = px_at(s, d0), px_at(s, d1)
            if p0 and p1 and p0 > 0:
                rets[c] = p1 / p0 - 1
        if len(rets) < 20:
            continue
        cover.append(len(rets) / max(len(codes[:40]), 1))
        for a, b, name in BUCKETS:
            vals = [rets[c] for c in codes[a:b] if c in rets]
            buckets[name].append(float(np.mean(vals)) if vals else np.nan)
        top20.append(float(np.mean([rets[c] for c in codes[:20] if c in rets])))
        top25.append(float(np.mean([rets[c] for c in codes[:25] if c in rets])))

    res = {name: stats(pd.Series(v)) for name, v in buckets.items()}
    d2125 = pd.Series(buckets["21-25"]) - pd.Series(buckets["26-30"])
    d20_25 = pd.Series(top20) - pd.Series(top25)
    out = {
        "n_months": len(top25),
        "avg_coverage_of_top40": round(float(np.mean(cover)), 3) if cover else None,
        "buckets": res,
        "diff_21_25_minus_26_30": stats(d2125),
        "diff_top20_minus_top25": stats(d20_25),
        "top20": stats(pd.Series(top20)),
        "top25": stats(pd.Series(top25)),
        "criterion_note": ("若 diff_21_25_minus_26_30 不显著(|t|<2)且分桶不单调"
                           "→ 边界换人无害, TOP25 维持; 若单调递减且 21-25 "
                           "显著高于 26-30 → 排名有效, TOP20 支持。"),
        "caveats": [
            "快照 dblow=price-only(溢价恒0), 与实盘同口径",
            "月收益为价格收益, 不含兑付/分红; 退市用最后收盘",
            "非回测: 不含月度调仓成本/临期剔除/停牌补位",
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: out[k] for k in
                      ("n_months", "avg_coverage_of_top40", "buckets",
                       "diff_21_25_minus_26_30", "diff_top20_minus_top25")},
                     ensure_ascii=False, indent=1, default=str))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
