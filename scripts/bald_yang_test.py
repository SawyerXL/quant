"""“光头阳线次日低开”检验（2026-10-25）。

定义：光头阳线 = 收盘>开盘 且 上影线 (high-close)/close ≤ 0.1%；
      长下影 = (min(open,close)-low)/close ≥ 1%。
输出：次日开盘缺口（open[t+1]/close[t]-1）与次日收益的分布。
只做描述，不给方向规则；样本重叠需按 N 读。
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("/data/quant_data_store/daily")


def load_index(code="SH000001"):
    frames = []
    for y in range(2000, 2027):
        p = DATA / str(y) / f"{code}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    d = pd.concat(frames).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def prep(d, upper_max=0.001):
    d = d.copy()
    d["r1"] = d["close"].pct_change()
    d["upper"] = (d["high"] - d["close"]) / d["close"]
    d["lower"] = (d[["open", "close"]].min(axis=1) - d["low"]) / d["close"]
    d["body"] = (d["close"] - d["open"]) / d["close"]
    d["bald_yang"] = (d["close"] > d["open"]) & (d["upper"] <= upper_max)
    d["long_lower"] = d["lower"] >= 0.01
    d["below200"] = d["close"] < d["close"].rolling(200).mean()
    d["ret20"] = d["close"] / d["close"].shift(20) - 1
    d["gap_next"] = d["open"].shift(-1) / d["close"] - 1
    d["ret_next"] = d["close"].shift(-1) / d["close"] - 1
    d["oc_next"] = d["close"].shift(-1) / d["open"].shift(-1) - 1
    d["gap_down"] = d["gap_next"] < 0
    d["next_down"] = d["ret_next"] < 0
    d["gapdown_closeup"] = (d["gap_next"] < 0) & (d["ret_next"] > 0)
    return d


def stats(d, mask, start):
    x = d[(d["date"] >= pd.Timestamp(start)) & mask].dropna(subset=["gap_next", "ret_next"])
    if len(x) == 0:
        return {"n": 0}
    return {
        "n": int(len(x)),
        "p_gap_down": round(float(x["gap_down"].mean()), 3),
        "gap_mean_pct": round(float(x["gap_next"].mean()) * 100, 3),
        "gap_median_pct": round(float(x["gap_next"].median()) * 100, 3),
        "p_next_down": round(float(x["next_down"].mean()), 3),
        "next_mean_pct": round(float(x["ret_next"].mean()) * 100, 3),
        "next_median_pct": round(float(x["ret_next"].median()) * 100, 3),
        "p_gapdown_closeup": round(float(x["gapdown_closeup"].mean()), 3),
        "oc_next_mean_pct": round(float(x["oc_next"].mean()) * 100, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="SH000001")
    ap.add_argument("--upper", type=float, default=0.001)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    d = prep(load_index(args.code), args.upper)
    out = {"code": args.code, "upper_max": args.upper, "sample": "2000-2026",
           "definitions": {
               "bald_yang": f"close>open and (high-close)/close<={args.upper:.2%}",
               "long_lower": "(min(open,close)-low)/close>=1%",
           }}
    for start in ["2000-01-01", "2012-01-01"]:
        out[start] = {
            "base": stats(d, pd.Series(True, index=d.index), start),
            "bald_yang": stats(d, d["bald_yang"], start),
            "bald_yang_long_lower": stats(d, d["bald_yang"] & d["long_lower"], start),
            "bald_long_below200": stats(d, d["bald_yang"] & d["long_lower"] & d["below200"], start),
            "bald_long_below200_ret20_neg": stats(
                d, d["bald_yang"] & d["long_lower"] & d["below200"] & (d["ret20"] < -0.02), start),
            "bald_long_deepv": stats(
                d, d["bald_yang"] & d["long_lower"] & (d["r1"].shift(1) <= -0.012), start),
        }
    x = d.iloc[-1]
    out["last_bar"] = {
        "date": str(x["date"].date()),
        "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]),
        "close": float(x["close"]), "upper_pct": round(float(x["upper"]) * 100, 3),
        "lower_pct": round(float(x["lower"]) * 100, 3), "body_pct": round(float(x["body"]) * 100, 2),
        "bald_yang": bool(x["bald_yang"]), "long_lower": bool(x["long_lower"]),
        "below200": bool(x["below200"]), "ret20_pct": round(float(x["ret20"]) * 100, 2),
    }
    p = Path(args.out) if args.out else Path(
        f"/root/quant/logs/bald_yang_test_{args.code}_20261025.json")
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
