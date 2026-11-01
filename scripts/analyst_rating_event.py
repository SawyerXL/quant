"""分析师评级变化/首次覆盖事件研究（2026-11-01）。

预登记 logs/research_registration_analyst_rating_20261101.md。
数据：CNINFO 分析师评级（akshare 逐日缓存）；事件=调高或首次评级，
发布日次一交易日 open 入场，20/60 日 close 退出，减 CSI800，扣 20bp；
发布日聚类 MDE。输出 logs/analyst_rating_20261101.json/.md。
"""
from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.storage import load_daily

CACHE_DIR = ROOT / "data_store" / "meta" / "analyst_ratings_cninfo"
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
INDEX = ROOT / "data_store" / "meta" / "csi800_index.parquet"
OUT_JSON = ROOT / "logs" / "analyst_rating_20261101.json"
OUT_MD = ROOT / "logs" / "analyst_rating_20261101.md"
START, END = "2019-01-01", "2026-10-30"
IS = ("2019-01-01", "2023-12-31")
OOS = ("2024-01-01", "2026-10-30")
EXCLUDE = {"000001", "000905", "000906", "000688"}
COST = 0.0020
HORIZONS = (20, 60)


def load_ratings() -> pd.DataFrame:
    files = sorted(CACHE_DIR.glob("*.parquet"))
    if not files:
        raise RuntimeError(f"no analyst cache in {CACHE_DIR}")
    frames = [pd.read_parquet(p) for p in files]
    d = pd.concat(frames, ignore_index=True)
    d["code"] = d["证券代码"].astype(str).str.zfill(6)
    d["date"] = pd.to_datetime(d["发布日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[d["date"].notna()]
    d = d[(d["date"] >= START) & (d["date"] <= END)]
    d = d[~d["code"].isin(EXCLUDE)]
    return d


def classify_events(d: pd.DataFrame) -> pd.DataFrame:
    """同一股票/发布日去重：有 positive 记 positive，否则 negative。"""
    d = d.copy()
    d["pos"] = ((d["评级变化"] == "调高")
                | (d["是否首次评级"] == "是首次评级"))
    d["neg"] = d["评级变化"] == "调低"
    rows = []
    for (code, date), g in d.groupby(["code", "date"]):
        if g["pos"].any():
            rows.append({"code": code, "date": date, "kind": "positive"})
        elif g["neg"].any():
            rows.append({"code": code, "date": date, "kind": "negative"})
    return pd.DataFrame(rows)


def membership_by_date(dates: list[str]) -> dict:
    w = pd.read_parquet(WEIGHTS)
    w["code"] = w["code"].astype(str).str.zfill(6)
    w["trade_date"] = pd.to_datetime(w["trade_date"]).dt.strftime("%Y-%m-%d")
    w = w[~w["code"].isin(EXCLUDE)]
    snaps = sorted(w["trade_date"].unique())
    sets = {s: set(g["code"]) for s, g in w.groupby("trade_date")}
    out = {}
    for d in dates:
        i = bisect.bisect_right(snaps, d) - 1
        out[d] = sets[snaps[i]] if i >= 0 else set()
    return out


def summarize(events: pd.DataFrame, start: str, end: str, col: str,
              kind: str) -> dict:
    x = events[(events["date"] >= start) & (events["date"] <= end)]
    x = x[x["kind"] == kind].dropna(subset=[col])
    if x.empty:
        return {"n_events": 0, "n_days": 0}
    daily = x.groupby("date")[col].mean()
    n, nd = len(x), len(daily)
    se = float(daily.std(ddof=1) / np.sqrt(nd)) if nd > 1 else float("nan")
    mde = 2.8 * se if nd > 1 else float("nan")
    return {
        "n_events": int(n), "n_days": int(nd),
        "mean_event_pct": round(float(x[col].mean()) * 100, 4),
        "mean_day_pct": round(float(daily.mean()) * 100, 4),
        "median_event_pct": round(float(x[col].median()) * 100, 4),
        "win_rate_pct": round(float((x[col] > 0).mean()) * 100, 2),
        "day_t": round(float(daily.mean() / se), 3) if nd > 1 and se > 0 else None,
        "mde_pct": round(float(mde) * 100, 4) if np.isfinite(mde) else None,
    }


def run() -> dict:
    raw = load_ratings()
    ev = classify_events(raw)
    snap = membership_by_date(sorted(ev["date"].unique()))
    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    idx_close = pd.to_numeric(idx.set_index("date")["close"], errors="coerce").dropna()

    rows = []
    for code, g in ev.groupby("code"):
        try:
            df = load_daily(code, "2018-06-01", END,
                            columns=["date", "open", "close"])
        except Exception:
            continue
        if len(df) < 100:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        ds = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        opens = pd.to_numeric(df["open"], errors="coerce").to_numpy()
        closes = pd.to_numeric(df["close"], errors="coerce").to_numpy()
        for _, r in g.iterrows():
            ann = r["date"]
            if code not in snap.get(ann, set()):
                continue
            ei = bisect.bisect_right(ds, ann)
            if ei + min(HORIZONS) >= len(df):
                continue
            entry_date = ds[ei]
            if entry_date not in idx_close.index or not np.isfinite(opens[ei]) \
                    or opens[ei] <= 0:
                continue
            row = {"code": code, "date": ann, "kind": r["kind"],
                   "entry": entry_date}
            for h in HORIZONS:
                xi = ei + h
                if xi >= len(df) or ds[xi] not in idx_close.index:
                    row[f"ex{h}"] = np.nan
                    continue
                raw_ret = closes[xi] / opens[ei] - 1
                mkt = idx_close.loc[ds[xi]] / idx_close.loc[entry_date] - 1
                row[f"ex{h}"] = raw_ret - mkt - COST
            rows.append(row)
    ev2 = pd.DataFrame(rows)
    out = {
        "as_of": END,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_analyst_rating_20261101.md",
        "source": "akshare.stock_rank_forecast_cninfo (CNINFO)",
        "n_raw": int(len(raw)), "n_events": int(len(ev2)),
        "n_positive": int((ev2["kind"] == "positive").sum()),
        "n_negative": int((ev2["kind"] == "negative").sum()),
        "n_dates": int(ev2["date"].nunique()),
        "periods": {},
    }
    for label, (s, e) in (("IS", IS), ("OOS", OOS), ("ALL", (START, END))):
        out["periods"][label] = {
            f"{kind}_ex{h}": summarize(ev2, s, e, f"ex{h}", kind)
            for kind in ("positive", "negative") for h in HORIZONS
        }
    pos = out["periods"]["OOS"]["positive_ex20"]
    ipos = out["periods"]["IS"]["positive_ex20"]
    if pos.get("n_days", 0) < 20 or pos.get("mde_pct") is None \
            or pos["mde_pct"] > 2.0:
        verdict = "UNDERPOWERED"
    elif pos.get("mean_day_pct", -999) > pos["mde_pct"] \
            and pos.get("mean_day_pct", -999) > 0 \
            and ipos.get("mean_day_pct", -999) > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    out["verdict"] = verdict
    out["rule"] = ("OOS positive 20d day-cluster mean > MDE and >0, IS same sign; "
                   "else FAIL; n_days<20 or MDE>2pp UNDERPOWERED")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 分析师评级变化/首次覆盖事件研究（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；数据 {out['source']}；"
        f"原始 {out['n_raw']} 行，事件 {out['n_events']} "
        f"(positive {out['n_positive']}/negative {out['n_negative']})，"
        f"发布日 {out['n_dates']}。",
        f"- **裁决：{verdict}**（{out['rule']}）。",
        "",
        "| 期间 | 方向 | 窗口 | n_events | n_days | 事件均值 | 日聚类均值 | 中位 | 胜率 | t | MDE |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("IS", "OOS", "ALL"):
        for kind in ("positive", "negative"):
            for h in HORIZONS:
                s = out["periods"][label][f"{kind}_ex{h}"]
                lines.append(
                    f"| {label} | {kind} | {h}d | {s.get('n_events',0)} | "
                    f"{s.get('n_days',0)} | {s.get('mean_event_pct')}% | "
                    f"{s.get('mean_day_pct')}% | {s.get('median_event_pct')}% | "
                    f"{s.get('win_rate_pct')}% | {s.get('day_t')} | "
                    f"{s.get('mde_pct')}% |"
                )
    lines += [
        "",
        "口径：发布日次一交易日 open 入场，20/60 日 close 退出；减 CSI800，"
        "扣 20bp；发布日聚类 MDE。positive=调高或首次评级；negative=调低。",
        "盲区：CNINFO/akshare 二手源、覆盖选择偏差、同一股票多机构重复、"
        "停牌/涨跌停/流动性未建模；事件稀疏可能欠功效；FAIL 即关闭，不调参。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return out


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
