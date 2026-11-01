"""C0007：1/3 回撤支撑的机械事件研究（预登记见 logs/research_registration_c0007_20261101.md）。

规则摘要：
- CSI800 PIT 成员（csi800_pit_weights 快照）；
- k=5 pivot（i±5），pivot 在 i+5 确认，无前视；
- 最近低点 A → 其后高点 B（B>A），S=A+(B-A)/3；
- B 确认后 60 日内首次 low<=S 为事件；先收盘跌破 A 作废；
- entry=min(open,S)（open<=S 用 open，否则用 S）；
- 5/10/20 日 forward，扣 20bp 往返，减 CSI800 同窗收益。
输出 logs/c0007_support_20261101.json/.md；FAIL 即关闭，不调参。
"""
from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.storage import load_daily

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
INDEX = ROOT / "data_store" / "meta" / "csi800_index.parquet"
OUT_JSON = ROOT / "logs" / "c0007_support_20261101.json"
OUT_MD = ROOT / "logs" / "c0007_support_20261101.md"
IS = ("2019-07-01", "2023-12-31")
OOS = ("2024-01-01", "2026-09-30")
EVENT_START = "2019-07-01"
EVENT_END = "2026-09-30"
DATA_START = "2018-06-01"
K = 5
COST = 0.0020
MAX_WAIT = 60
EXCLUDE = {"000001", "000905", "000906", "000688"}  # 指数/个股歧义键


def pivots(df: pd.DataFrame, k: int = K):
    """返回 (low_pivots, high_pivots)，索引为 pivot 所在行（i±k 局部极值）。"""
    low = pd.to_numeric(df["low"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    roll_low = low.rolling(2 * k + 1, center=True, min_periods=2 * k + 1).min()
    roll_high = high.rolling(2 * k + 1, center=True, min_periods=2 * k + 1).max()
    low_piv = np.where(
        (low == roll_low) & (low < low.shift(1)) & (low <= low.shift(-1))
    )[0]
    high_piv = np.where(
        (high == roll_high) & (high > high.shift(1)) & (high >= high.shift(-1))
    )[0]
    return low_piv.tolist(), high_piv.tolist()


def find_setups(df: pd.DataFrame, k: int = K):
    """按确认顺序生成 low→high setup；返回 [{a_i,a,b_i,b,s,confirm_i}]。"""
    low_p, high_p = pivots(df, k)
    piv = [(i + k, "L", i) for i in low_p] + [(i + k, "H", i) for i in high_p]
    piv.sort()
    last_low = None
    setups = []
    for conf, typ, i in piv:
        if typ == "L":
            last_low = (i, float(df["low"].iloc[i]))
        elif last_low is not None:
            b = float(df["high"].iloc[i])
            a = last_low[1]
            if b > a:
                setups.append({
                    "a_i": last_low[0], "a": a, "b_i": i, "b": b,
                    "s": a + (b - a) / 3.0, "confirm_i": conf,
                })
                last_low = None
    return setups


def first_touch(df: pd.DataFrame, setup: dict, max_wait: int = MAX_WAIT):
    """返回 (touch_i, entry) 或 None；先收盘跌破 A 作废。"""
    n = len(df)
    start = setup["confirm_i"] + 1
    end = min(start + max_wait, n)
    for t in range(start, end):
        if float(df["close"].iloc[t]) < setup["a"]:
            return None
        if float(df["low"].iloc[t]) <= setup["s"]:
            op = float(df["open"].iloc[t])
            entry = op if op <= setup["s"] else setup["s"]
            if entry > 0:
                return t, entry
            return None
    return None


def summarize(events: pd.DataFrame, start: str, end: str, col: str) -> dict:
    x = events[(events["date"] >= start) & (events["date"] <= end)].dropna(subset=[col])
    if x.empty:
        return {"n_events": 0, "n_days": 0}
    daily = x.groupby("date")[col].mean()
    n, nd = len(x), len(daily)
    se = float(daily.std(ddof=1) / np.sqrt(nd)) if nd > 1 else float("nan")
    mde = 2.8 * se if nd > 1 else float("nan")
    return {
        "n_events": int(n),
        "n_days": int(nd),
        "mean_event_pct": round(float(x[col].mean()) * 100, 4),
        "median_event_pct": round(float(x[col].median()) * 100, 4),
        "mean_day_pct": round(float(daily.mean()) * 100, 4),
        "median_day_pct": round(float(daily.median()) * 100, 4),
        "win_rate_pct": round(float((x[col] > 0).mean()) * 100, 2),
        "day_t": round(float(daily.mean() / se), 3) if nd > 1 and se > 0 else None,
        "mde_pct": round(float(mde) * 100, 4) if np.isfinite(mde) else None,
    }


def run(k: int = K) -> dict:
    w = pd.read_parquet(WEIGHTS)
    w["code"] = w["code"].astype(str).str.zfill(6)
    w["trade_date"] = pd.to_datetime(w["trade_date"]).dt.strftime("%Y-%m-%d")
    w = w[w["code"].isin(EXCLUDE) == False]
    snap_dates = sorted(w["trade_date"].unique())
    membership = {d: set(g["code"]) for d, g in w.groupby("trade_date")}

    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    idx = pd.to_numeric(idx.set_index("date")["close"], errors="coerce").dropna()

    codes = sorted(w["code"].unique())
    rows = []
    n_loaded = 0
    for code in codes:
        df = load_daily(code, DATA_START, EVENT_END, columns=["date", "open", "high", "low", "close"])
        if len(df) < 300:
            continue
        df = df.reset_index(drop=True)
        df["d"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        n_loaded += 1
        for st in find_setups(df, k):
            hit = first_touch(df, st)
            if hit is None:
                continue
            t, entry = hit
            d0 = df["d"].iloc[t]
            if d0 < EVENT_START or d0 > EVENT_END:
                continue
            si = bisect.bisect_right(snap_dates, d0) - 1
            if si < 0 or code not in membership.get(snap_dates[si], set()):
                continue
            row = {"date": d0, "code": code, "entry": entry,
                   "a": st["a"], "b": st["b"], "s": st["s"]}
            ok = True
            for h in (5, 10, 20):
                if t + h >= len(df):
                    ok = False
                    break
                d1 = df["d"].iloc[t + h]
                if d0 not in idx.index or d1 not in idx.index:
                    ok = False
                    break
                raw = float(df["close"].iloc[t + h]) / entry - 1
                mkt = float(idx.loc[d1]) / float(idx.loc[d0]) - 1
                row[f"ret{h}"] = raw
                row[f"ex{h}"] = raw - mkt - COST
            if ok:
                rows.append(row)
        if n_loaded % 200 == 0:
            print("loaded", n_loaded, "events", len(rows), flush=True)
    ev = pd.DataFrame(rows)
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_c0007_20261101.md",
        "k": k,
        "cost_roundtrip": COST,
        "universe": "CSI800 PIT weights",
        "codes_loaded": n_loaded,
        "n_setups_events": len(ev),
        "periods": {},
    }
    for label, (s, e) in (("IS", IS), ("OOS", OOS), ("ALL", (IS[0], OOS[1]))):
        out["periods"][label] = {
            h: summarize(ev, s, e, f"ex{h}") for h in (5, 10, 20)
        }
    primary = out["periods"]["OOS"][10]
    is10 = out["periods"]["IS"][10]
    if primary.get("n_events", 0) < 100 or primary.get("mde_pct") is None:
        verdict = "UNDERPOWERED"
    elif primary.get("mde_pct", 0) and primary["mean_day_pct"] > primary["mde_pct"] \
            and is10.get("mean_day_pct", 0) > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    out["verdict"] = verdict
    out["rule"] = "OOS 10d ex_net mean > MDE and IS same sign; else FAIL"
    return out


def render_md(d: dict) -> str:
    lines = [
        "# C0007 1/3 回撤支撑检验（机械代理）",
        "",
        f"- 预登记：`{d['registration']}`；k={d['k']}；成本 {d['cost_roundtrip']*1e4:.0f}bp；"
        f"事件 {d['n_setups_events']}；载入 {d['codes_loaded']} 只。",
        f"- 裁决：**{d['verdict']}**（{d['rule']}）。",
        "",
        "| 期间 | horizon | n_events | n_days | 事件均值 | 日聚类均值 | 事件中位 | 胜率 | t | MDE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("IS", "OOS", "ALL"):
        for h in (5, 10, 20):
            s = d["periods"][label][h]
            lines.append(
                f"| {label} | {h}d | {s.get('n_events',0)} | {s.get('n_days',0)} | "
                f"{s.get('mean_event_pct')}% | {s.get('mean_day_pct')}% | "
                f"{s.get('median_event_pct')}% | {s.get('win_rate_pct')}% | "
                f"{s.get('day_t')} | {s.get('mde_pct')}% |"
            )
    lines += [
        "",
        "局限：机械 pivot 不等于视频作者主观画线；PIT 快照非逐日；"
        "停牌/涨跌停/限价排队未建模；单次检验不单独作为部署依据。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    d = run(K)
    OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_MD.write_text(render_md(d), encoding="utf-8")
    print(render_md(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
