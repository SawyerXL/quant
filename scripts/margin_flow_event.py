"""融资余额拥挤度（margin flow）月度截面事件研究（2026-11-01）。

预登记 logs/research_registration_margin_flow_20261101.md。
信号=融资余额 / 20 日均成交额；月末截面分十组；低杠杆组次月 open
入场、20 日 close 退出，减 CSI800，扣 20bp；月末聚类 MDE。
数据源 akshare 沪深融资融券明细（2019+）；只作研究，不进信号链。
输出 logs/margin_flow_20261101.json/.md。
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

CACHE = ROOT / "data_store" / "meta" / "margin_month_end.parquet"
META = ROOT / "data_store" / "meta" / "margin_month_end.meta.json"
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
INDEX = ROOT / "data_store" / "meta" / "csi800_index.parquet"
OUT_JSON = ROOT / "logs" / "margin_flow_20261101.json"
OUT_MD = ROOT / "logs" / "margin_flow_20261101.md"
START, END = "2019-01-01", "2026-10-30"
IS = ("2019-01-01", "2023-12-31")
OOS = ("2024-01-01", "2026-10-30")
EXCLUDE = {"000001", "000905", "000906", "000688"}
COST = 0.0020
HORIZONS = (20, 60)


def month_ends() -> list[str]:
    sh = load_daily("SH000001", "2018-12-01", END, columns=["date"])
    dates = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    out = {}
    for d in dates:
        if START <= d <= END:
            out[d[:7]] = d
    return sorted(out.values())


def fetch_margin(refresh: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pd.read_parquet(CACHE)
    import akshare as ak
    rows = []
    for d in month_ends():
        ds = d.replace("-", "")
        for fn, code_col in (("stock_margin_detail_sse", "标的证券代码"),
                             ("stock_margin_detail_szse", "证券代码")):
            try:
                x = getattr(ak, fn)(ds)
                if x is None or x.empty:
                    continue
                x = x.copy()
                x["code"] = x[code_col].astype(str).str.zfill(6)
                x["mb"] = pd.to_numeric(x["融资余额"], errors="coerce")
                x = x[["code", "mb"]].dropna()
                x["date"] = d
                rows.append(x)
            except Exception as e:
                print("fetch fail", d, fn, type(e).__name__, str(e)[:80],
                      flush=True)
        print("margin", d, sum(len(x) for x in rows), flush=True)
    if not rows:
        raise RuntimeError("no margin data")
    out = pd.concat(rows, ignore_index=True)
    out = out.groupby(["date", "code"], as_index=False)["mb"].max()
    out.to_parquet(CACHE, index=False)
    META.write_text(json.dumps({
        "source": "akshare SSE/SZSE stock_margin_detail",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": len(month_ends()),
        "rows": len(out),
        "note": "month-end margin balance; research only",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


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


def assign_decile(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for d, g in df.groupby("date"):
        if len(g) < 20:
            continue
        try:
            g = g.assign(dec=pd.qcut(g["ratio"], 10, labels=False,
                                     duplicates="drop"))
        except ValueError:
            continue
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else df.iloc[0:0]


def summarize(events: pd.DataFrame, start: str, end: str, col: str,
              dec: int) -> dict:
    x = events[(events["date"] >= start) & (events["date"] <= end)]
    x = x[x["dec"] == dec].dropna(subset=[col])
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


def run(refresh: bool = False) -> dict:
    raw = fetch_margin(refresh)
    dates = sorted(raw["date"].unique())
    member = membership_by_date(dates)
    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    idx_close = pd.to_numeric(idx.set_index("date")["close"], errors="coerce").dropna()

    # 只保留 CSI800 成员；按股票加载日线，计算量价与前瞻收益。
    raw = raw[raw.apply(lambda r: r["code"] in member.get(r["date"], set()), axis=1)]
    rows = []
    by_code = {c: g for c, g in raw.groupby("code")}
    for code, g in by_code.items():
        try:
            df = load_daily(code, "2018-06-01", END,
                            columns=["date", "open", "close", "amount"])
        except Exception:
            continue
        if len(df) < 100:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        ds = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        opens = pd.to_numeric(df["open"], errors="coerce").to_numpy()
        closes = pd.to_numeric(df["close"], errors="coerce").to_numpy()
        amts = pd.to_numeric(df["amount"], errors="coerce").to_numpy()
        for _, r in g.iterrows():
            d = r["date"]
            pos = bisect.bisect_right(ds, d) - 1
            if pos < 20 or not np.isfinite(amts[pos - 19:pos + 1]).all():
                continue
            avg_amt = float(np.mean(amts[pos - 19:pos + 1]))
            if avg_amt <= 0 or not np.isfinite(r["mb"]):
                continue
            ei = pos + 1  # 月末次一交易日 open
            if ei + min(HORIZONS) >= len(df):
                continue
            entry_date = ds[ei]
            if entry_date not in idx_close.index or not np.isfinite(opens[ei]) \
                    or opens[ei] <= 0:
                continue
            row = {"date": d, "code": code, "ratio": float(r["mb"]) / avg_amt,
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
    ev = assign_decile(pd.DataFrame(rows))
    out = {
        "as_of": END,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_margin_flow_20261101.md",
        "source": "akshare SSE/SZSE margin detail",
        "n_raw": int(len(raw)), "n_events": int(len(ev)),
        "n_dates": int(ev["date"].nunique()),
        "periods": {},
    }
    for label, (s, e) in (("IS", IS), ("OOS", OOS), ("ALL", (START, END))):
        out["periods"][label] = {
            f"dec{d}_ex{h}": summarize(ev, s, e, f"ex{h}", d)
            for d in (0, 9) for h in HORIZONS
        }
    low = out["periods"]["OOS"]["dec0_ex20"]
    ilow = out["periods"]["IS"]["dec0_ex20"]
    if low.get("n_days", 0) < 20 or low.get("mde_pct") is None \
            or low["mde_pct"] > 2.0:
        verdict = "UNDERPOWERED"
    elif low.get("mean_day_pct", -999) > low["mde_pct"] \
            and low.get("mean_day_pct", -999) > 0 \
            and ilow.get("mean_day_pct", -999) > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    out["verdict"] = verdict
    out["rule"] = ("OOS low-decile 20d ex mean > MDE and >0, IS same sign; "
                   "else FAIL; n_days<20 or MDE>2pp UNDERPOWERED")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 融资余额拥挤度（margin flow）事件研究（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；数据 {out['source']}；"
        f"原始 {out['n_raw']} 行，事件 {out['n_events']}，月末 {out['n_dates']}。",
        f"- **裁决：{verdict}**（{out['rule']}）。",
        "",
        "| 期间 | 组 | 窗口 | n_events | n_days | 事件均值 | 日聚类均值 | 胜率 | t | MDE |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("IS", "OOS", "ALL"):
        for dec in (0, 9):
            for h in HORIZONS:
                s = out["periods"][label][f"dec{dec}_ex{h}"]
                lines.append(
                    f"| {label} | {'低' if dec==0 else '高'} | {h}d | "
                    f"{s.get('n_events',0)} | {s.get('n_days',0)} | "
                    f"{s.get('mean_event_pct')}% | {s.get('mean_day_pct')}% | "
                    f"{s.get('win_rate_pct')}% | {s.get('day_t')} | "
                    f"{s.get('mde_pct')}% |"
                )
    lines += [
        "",
        "口径：月末 `融资余额 / 20 日均成交额` 分十组；次一交易日 open 入场，"
        "20/60 日 close 退出；减 CSI800，扣 20bp；月末聚类 MDE。",
        "盲区：akshare 二手交易所数据、月末频率、PIT 快照非逐日、"
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
