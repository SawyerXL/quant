"""洗盘事件 census（只数事件，不看任何前向收益）— 2026-10-25。

预注册：`logs/prereg_washout_20261025.md`（N=79）。
本脚本**禁止 join 收益**：只输出事件数、参数分布、剔除计数、覆盖率。
MDE 锁定后才允许跑 `washout_event_study.py` 的收益段。

宇宙：CSI800 PIT 季度成分（2019-02-01~2026-10-23），指数编制剔除 ST。
事件：`chip_structure.classify_metrics` 的 WASHOUT_WATCH（参数 P 冻结）。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from chip_structure import P, load_float_shares  # noqa: E402
from data.storage import load_daily  # noqa: E402

WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
LOG_DIR = ROOT / "logs"


def load_membership():
    w = pd.read_parquet(WEIGHTS)
    w["trade_date"] = pd.to_datetime(w["trade_date"]).dt.normalize()
    w["code"] = w["code"].astype(str).str.zfill(6)
    snap = {d: set(g["code"]) for d, g in w.groupby("trade_date")}
    return snap, sorted(snap)


def is_member(code: str, t: pd.Timestamp, snap: dict, snap_dates: list) -> bool:
    i = np.searchsorted(np.array(snap_dates, dtype="datetime64[ns]"),
                        np.datetime64(t), side="right") - 1
    return i >= 0 and code in snap[snap_dates[i]]


def board_limit(code: str) -> float:
    return 0.195 if code.startswith(("300", "301", "688")) else 0.095


def turnover_range(turn: np.ndarray, valid: np.ndarray, i0: int, i1: int):
    """[i0,i1] 累积换手；返回 (sum, 有效天数, 总天数)。"""
    s = turn[i0:i1 + 1]
    ok = valid[i0:i1 + 1]
    if ok.sum() == 0:
        return None, 0, len(s)
    return float(np.nansum(s[ok])), int(ok.sum()), len(s)


def scan_code(code: str, snap: dict, snap_dates: list, start: pd.Timestamp,
              end: pd.Timestamp) -> dict:
    out = {"code": code, "bars": 0, "missing": False, "events": [],
           "cand_price": 0, "cand_float_missing": 0, "cand_turnover_low": 0,
           "skipped_member": 0, "skipped_liquidity": 0}
    d = load_daily(code, "2015-01-01", end.strftime("%Y-%m-%d"))
    if d is None or len(d) == 0:
        out["missing"] = True
        return out
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] <= end].sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"]).reset_index(drop=True)
    n = len(d)
    out["bars"] = n
    if n < 300:
        return out

    fs, fs_status = load_float_shares(code, asof=end.strftime("%Y-%m-%d"))
    if fs is not None and len(fs):
        f = fs.drop_duplicates("date").sort_values("date")
        d = pd.merge_asof(d, f, on="date", direction="backward")
    else:
        d["float_shares"] = np.nan
    out["float_status"] = fs_status
    close = d["close"].to_numpy(float)
    volume = d["volume"].to_numpy(float)
    amount = d["amount"].to_numpy(float)
    float_sh = pd.to_numeric(d["float_shares"], errors="coerce").to_numpy(float)
    valid_float = np.isfinite(float_sh) & (float_sh > 0)
    turn = np.where(valid_float, volume * 1e4 / np.where(valid_float, float_sh, 1), np.nan)

    # 250 日滚动高点的“最后一次出现”位置
    w = 250
    sw = np.lib.stride_tricks.sliding_window_view(close, w)  # 行 i 对应 t=i+249
    rev_arg = np.argmax(sw[:, ::-1], axis=1)
    hi_rel = (w - 1 - rev_arg).astype(np.int32)
    hi_val = sw[np.arange(len(sw)), hi_rel]
    t_idx = np.arange(w - 1, n)
    days = (w - 1 - hi_rel)
    dd = close[t_idx] / hi_val - 1

    # 流动性：20 日均额 ≥5000 万元（amount 单位=万元）
    liq = pd.Series(amount).rolling(20, min_periods=10).mean().to_numpy()[t_idx]
    base_mask = ((days >= P["days_min"]) & (days <= P["days_max"])
                 & (dd >= P["dd_min"]) & (dd <= P["dd_max"])
                 & (t_idx >= 300) & np.isfinite(liq) & (liq >= 5000))
    for j in np.where(base_mask)[0]:
        t = int(t_idx[j])
        td = d["date"].iloc[t]
        if td < start or td > end:
            continue
        if not is_member(code, td, snap, snap_dates):
            out["skipped_member"] += 1
            continue
        hi_pos = t - (w - 1) + int(hi_rel[j])
        prior_start = max(0, hi_pos - (w - 1))
        prior_low = float(np.nanmin(close[prior_start:hi_pos + 1]))
        prior_leg = float(hi_val[j]) / prior_low - 1 if prior_low > 0 else np.nan
        if not np.isfinite(prior_leg) or prior_leg < P["prior_leg_min"]:
            continue
        seg = close[hi_pos:t + 1]
        rw = float(np.nanmax(seg) / np.nanmin(seg) - 1)
        if not np.isfinite(rw) or rw > P["range_width_max"]:
            continue
        out["cand_price"] += 1
        to, to_ok, to_n = turnover_range(turn, valid_float, hi_pos, t)
        if to is None:
            out["cand_float_missing"] += 1
            continue
        if to < P["turnover_min"]:
            out["cand_turnover_low"] += 1
            continue
        # T+1 / T+60 可执行性（只做计数，不看收益）
        mature = (t + 60) < n
        t1_ok = (t + 1) < n
        limit_up = False
        if t1_ok:
            lim = board_limit(code)
            limit_up = bool(d["open"].iloc[t + 1] >= close[t] * (1 + lim) - 1e-9)
        vol = volume
        vc = None
        if t >= 50:
            recent = np.nanmean(vol[t - 9:t + 1])
            basev = np.nanmean(vol[t - 49:t - 9])
            vc = float(recent / basev) if basev > 0 else None
        out["events"].append({
            "code": code, "date": td.strftime("%Y-%m-%d"), "hi_pos": hi_pos,
            "prior_leg": round(prior_leg, 4), "days_since_high": int(days[j]),
            "dd": round(float(dd[j]), 4), "range_width": round(rw, 4),
            "cum_turnover": round(float(to), 4), "turnover_ok_days": to_ok,
            "turnover_days": to_n, "vol_contraction": None if vc is None else round(vc, 4),
            "float_status": fs_status, "mature_60": bool(mature),
            "limit_up_t1": bool(limit_up), "amount_20d": round(float(liq[j]), 1),
        })
    return out


def dedup(events: list[dict]) -> list[dict]:
    """同一标的 60 个交易日内只保留首个事件（去重叠）。"""
    events.sort(key=lambda e: e["date"])
    kept, last = [], None
    for e in events:
        dt = pd.Timestamp(e["date"])
        if last is None or (dt - last).days >= 60 * 7 / 5:  # 60 交易日≈84 日历日
            kept.append(e)
            last = dt
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-02-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--limit", type=int, default=0, help="仅调试：限制股票数")
    args = ap.parse_args()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    snap, snap_dates = load_membership()
    codes = sorted(set().union(*snap.values()))
    if args.limit:
        codes = codes[:args.limit]
    logger.info(f"universe={len(codes)} snapshots={len(snap_dates)} "
                f"window={start.date()}~{end.date()}")

    all_events, stats = [], {"missing": [], "bars_lt300": [], "by_code": {}}
    for i, code in enumerate(codes, 1):
        try:
            r = scan_code(code, snap, snap_dates, start, end)
        except Exception as e:
            logger.warning(f"{code} scan fail: {type(e).__name__} {str(e)[:80]}")
            stats["missing"].append(code)
            continue
        if r["missing"]:
            stats["missing"].append(code)
            continue
        if r["bars"] < 300:
            stats["bars_lt300"].append(code)
            continue
        ev = dedup(r["events"])
        all_events.extend(ev)
        stats["by_code"][code] = {
            "bars": r["bars"], "cand_price": r["cand_price"],
            "float_missing": r["cand_float_missing"],
            "turnover_low": r["cand_turnover_low"],
            "skipped_member": r["skipped_member"], "events": len(ev),
            "float_status": r.get("float_status"),
        }
        if i % 100 == 0:
            logger.info(f"  {i}/{len(codes)} events={len(all_events)}")

    df = pd.DataFrame(all_events)
    if len(df):
        df = df.sort_values(["date", "code"]).reset_index(drop=True)
        df.to_csv(LOG_DIR / "washout_census_events_20261025.csv", index=False)

    def _dist(col):
        if not len(df) or col not in df:
            return None
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        return {q: round(float(s.quantile(q)), 4) for q in [0.1, 0.25, 0.5, 0.75, 0.9]} | {"n": int(len(s))}

    by_year = df.groupby(df["date"].str[:4]).size().to_dict() if len(df) else {}
    by_month = df.groupby(df["date"].str[:7]).size().to_dict() if len(df) else {}
    out = {
        "as_of": args.end,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": P,
        "universe_codes": len(codes),
        "snapshots": len(snap_dates),
        "window": [args.start, args.end],
        "codes_missing_bars": len(stats["missing"]),
        "codes_bars_lt300": len(stats["bars_lt300"]),
        "funnel": {
            "price_candidates": sum(v["cand_price"] for v in stats["by_code"].values()),
            "float_missing": sum(v["float_missing"] for v in stats["by_code"].values()),
            "turnover_low": sum(v["turnover_low"] for v in stats["by_code"].values()),
            "events_dedup": int(len(df)),
            "events_mature60": int(df["mature_60"].sum()) if len(df) else 0,
            "events_tradable": int((df["mature_60"] & ~df["limit_up_t1"]).sum()) if len(df) else 0,
            "unique_stocks": int(df["code"].nunique()) if len(df) else 0,
            "event_months": int(df["date"].str[:7].nunique()) if len(df) else 0,
        },
        "by_year": by_year,
        "by_month": by_month,
        "distributions": {c: _dist(c) for c in
                          ["prior_leg", "days_since_high", "dd", "range_width",
                           "cum_turnover", "vol_contraction"]},
        "excluded_member": sum(v["skipped_member"] for v in stats["by_code"].values()),
        "per_code": stats["by_code"],
    }
    p = LOG_DIR / "washout_census_20261025.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: out[k] for k in
                      ["universe_codes", "codes_missing_bars", "codes_bars_lt300",
                       "funnel", "by_year", "distributions"]},
                     ensure_ascii=False, indent=2, default=str))
    print("->", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
