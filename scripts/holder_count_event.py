"""股东户数变化（筹码集中）事件研究（2026-11-01）。

预登记 logs/research_registration_holder_count_20261101.md。
数据源 akshare `stock_zh_a_gdhs`（东财季度快照，含公告日期）；
只作研究，不进生产信号链。事件=公告日后首个交易日 open 入场，
20 日 close 退出，减 CSI800 同窗，扣 20bp；公告日聚类 MDE。
输出 logs/holder_count_20261101.json/.md。
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.storage import load_meta, load_daily

CACHE = ROOT / "data_store" / "meta" / "holder_count_em.parquet"
META = ROOT / "data_store" / "meta" / "holder_count_em.meta.json"
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
INDEX = ROOT / "data_store" / "meta" / "csi800_index.parquet"
OUT_JSON = ROOT / "logs" / "holder_count_20261101.json"
OUT_MD = ROOT / "logs" / "holder_count_20261101.md"
START, END = "2019-01-01", "2026-10-30"
IS = ("2019-01-01", "2023-12-31")
OOS = ("2024-01-01", "2026-10-30")
QUARTERS = [d.strftime("%Y%m%d") for d in
            pd.date_range("2019-03-31", "2026-06-30", freq="QE")]
EXCLUDE = {"000001", "000905", "000906", "000688"}
COST = 0.0020
HORIZONS = (20, 60)


def fetch_all(refresh: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pd.read_parquet(CACHE)
    import akshare as ak
    frames = []
    for q in QUARTERS:
        try:
            d = ak.stock_zh_a_gdhs(q)
            if d is None or d.empty:
                continue
            d = d.copy()
            d["quarter"] = q
            frames.append(d)
            print("fetched", q, len(d), flush=True)
        except Exception as e:
            print("fetch fail", q, type(e).__name__, str(e)[:100], flush=True)
    if not frames:
        raise RuntimeError("no holder-count data fetched")
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(CACHE, index=False)
    META.write_text(json.dumps({
        "source": "akshare.stock_zh_a_gdhs (Eastmoney)",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quarters": QUARTERS,
        "rows": len(out),
        "note": "quarterly snapshots with 公告日期; research only",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def prepare_events(raw: pd.DataFrame) -> pd.DataFrame:
    d = raw.copy()
    d["code"] = d["代码"].astype(str).str.zfill(6)
    d["chg"] = pd.to_numeric(d["股东户数-增减比例"], errors="coerce")
    d["ann"] = pd.to_datetime(d["公告日期"], errors="coerce")
    d["stat"] = pd.to_datetime(d["股东户数统计截止日-本次"], errors="coerce")
    d = d[d["chg"].notna() & d["ann"].notna()]
    d = d[(d["ann"] >= START) & (d["ann"] <= END)]
    d = d[~d["code"].isin(EXCLUDE)]
    d = d.sort_values(["code", "ann", "stat"]).drop_duplicates(
        ["code", "ann"], keep="last")
    return d[["code", "chg", "ann"]].reset_index(drop=True)


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


def assign_top_quintile(events: pd.DataFrame) -> pd.DataFrame:
    """按公告日横截面取 chg 最集中 20%（chg 越负越集中）。"""
    out = []
    for ann, g in events.groupby("date"):
        g = g.dropna(subset=["chg"])
        if len(g) < 10:
            continue
        rank = g["chg"].rank(method="first", ascending=True)
        g = g.assign(top=(rank <= max(2, int(len(g) * 0.2))).astype(int))
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else events.iloc[0:0]


def summarize(events: pd.DataFrame, start: str, end: str, col: str) -> dict:
    x = events[(events["date"] >= start) & (events["date"] <= end)].dropna(subset=[col])
    x = x[x["top"] == 1]
    if x.empty:
        return {"n_events": 0, "n_days": 0}
    daily = x.groupby("date")[col].mean()
    n, nd = len(x), len(daily)
    se = float(daily.std(ddof=1) / np.sqrt(nd)) if nd > 1 else float("nan")
    mde = 2.8 * se if nd > 1 else float("nan")
    return {
        "n_events": int(n), "n_days": int(nd),
        "mean_event_pct": round(float(x[col].mean()) * 100, 4),
        "median_event_pct": round(float(x[col].median()) * 100, 4),
        "mean_day_pct": round(float(daily.mean()) * 100, 4),
        "win_rate_pct": round(float((x[col] > 0).mean()) * 100, 2),
        "day_t": round(float(daily.mean() / se), 3) if nd > 1 and se > 0 else None,
        "mde_pct": round(float(mde) * 100, 4) if np.isfinite(mde) else None,
    }


def run(refresh: bool = False) -> dict:
    raw = fetch_all(refresh)
    ev = prepare_events(raw)
    snap = membership_by_date(sorted(ev["ann"].dt.strftime("%Y-%m-%d").unique()))
    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    idx_close = pd.to_numeric(idx.set_index("date")["close"], errors="coerce").dropna()

    rows = []
    by_code = {c: g for c, g in ev.groupby("code")}
    for code, g in by_code.items():
        try:
            df = load_daily(code, "2018-06-01", END, columns=["date", "open", "close"])
        except Exception:
            continue
        if len(df) < 100:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        ds = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        opens = pd.to_numeric(df["open"], errors="coerce").to_numpy()
        closes = pd.to_numeric(df["close"], errors="coerce").to_numpy()
        for _, r in g.iterrows():
            ann = r["ann"].strftime("%Y-%m-%d")
            if code not in snap.get(ann, set()):
                continue
            ei = bisect.bisect_right(ds, ann)
            if ei + min(HORIZONS) >= len(df):
                continue
            entry_date = ds[ei]
            if entry_date not in idx_close.index or not np.isfinite(opens[ei]) \
                    or opens[ei] <= 0:
                continue
            row = {"code": code, "date": ann, "entry": entry_date,
                   "chg": float(r["chg"])}
            for h in HORIZONS:
                xi = ei + h
                if xi >= len(df):
                    row[f"ex{h}"] = np.nan
                    continue
                exit_date = ds[xi]
                if exit_date not in idx_close.index:
                    row[f"ex{h}"] = np.nan
                    continue
                raw_ret = closes[xi] / opens[ei] - 1
                mkt = idx_close.loc[exit_date] / idx_close.loc[entry_date] - 1
                row[f"raw{h}"] = raw_ret
                row[f"ex{h}"] = raw_ret - mkt - COST
            rows.append(row)
        if len(rows) % 2000 < len(g):
            print("events", len(rows), flush=True)
    ev2 = pd.DataFrame(rows)
    if ev2.empty:
        raise RuntimeError("no event rows")
    ev2 = assign_top_quintile(ev2)
    out = {
        "as_of": END,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_holder_count_20261101.md",
        "source": "akshare.stock_zh_a_gdhs (Eastmoney)",
        "n_raw": int(len(raw)), "n_events": int(len(ev2)),
        "n_dates": int(ev2["date"].nunique()),
        "periods": {},
    }
    for label, (s, e) in (("IS", IS), ("OOS", OOS), ("ALL", (START, END))):
        out["periods"][label] = {f"ex{h}": summarize(ev2, s, e, f"ex{h}")
                                 for h in HORIZONS}
    oos = out["periods"]["OOS"]["ex20"]
    iss = out["periods"]["IS"]["ex20"]
    if oos.get("n_days", 0) < 20 or oos.get("mde_pct") is None \
            or oos["mde_pct"] > 2.0:
        verdict = "UNDERPOWERED"
    elif oos.get("mean_day_pct", -999) > oos["mde_pct"] \
            and oos.get("mean_day_pct", -999) > 0 \
            and iss.get("mean_day_pct", -999) > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    out["verdict"] = verdict
    out["rule"] = ("OOS 20d top-quintile ex mean > MDE and >0, IS same sign; "
                   "else FAIL; n_days<20 or MDE>2pp UNDERPOWERED")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 股东户数变化事件研究（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；数据 {out['source']}；"
        f"原始 {out['n_raw']} 行，事件 {out['n_events']}，公告日 {out['n_dates']}。",
        f"- **裁决：{verdict}**（{out['rule']}）。",
        "",
        "| 期间 | 窗口 | n_events | n_days | 事件均值 | 日聚类均值 | 中位 | 胜率 | t | MDE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("IS", "OOS", "ALL"):
        for h in HORIZONS:
            s = out["periods"][label][f"ex{h}"]
            lines.append(
                f"| {label} | {h}d | {s.get('n_events',0)} | {s.get('n_days',0)} | "
                f"{s.get('mean_event_pct')}% | {s.get('mean_day_pct')}% | "
                f"{s.get('median_event_pct')}% | {s.get('win_rate_pct')}% | "
                f"{s.get('day_t')} | {s.get('mde_pct')}% |"
            )
    lines += [
        "",
        "口径：公告日后首个交易日 open 入场，20/60 日 close 退出；"
        "减 CSI800 同窗 close，扣 20bp；公告日聚类 MDE=2.8×SE；"
        "quintile 1=股东户数降幅最大 20%。",
        "盲区：akshare 东财二手源、公告日期可能滞后、季度频率、PIT 快照非逐日、"
        "停牌/涨跌停/流动性未建模；FAIL 即关闭，不调参。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    run(args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
