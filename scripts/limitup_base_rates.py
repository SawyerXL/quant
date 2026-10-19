"""R1 第一阶段: 次日涨停基率(描述性, 2026-10-19 立项).

口径(出数前锁):
  - 涨停价 = round(前收 × (1+幅度), 2); 幅度按板块 10%/20%/30%, ST 5%.
  - 事件: T+1 触板(high ≥ 涨停价) / T+1 收盘封板(close ≥ 涨停价).
  - 条件基率特征(锁定清单): 近5日涨停次数 / T日涨幅档 / T日成交额分位 /
    T日炸板 / 行业当日涨停家数 / 市场当日涨停家数.
  - 样本<200 的条件不进入第二阶段.
用法:
  python scripts/limitup_base_rates.py --sample 300   # 管线验证(部分宇宙)
  python scripts/limitup_base_rates.py                # 全宇宙(正式)
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"


def band_of(code: str, is_st: bool) -> float:
    if is_st:
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def wilson(p, n, z=1.96):
    if n == 0:
        return (None, None)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="0=全宇宙")
    args = ap.parse_args()

    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st_map = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    ind_map = dict(zip(meta["code"], meta.get("industry_l1", "")))
    codes = sorted(meta["code"])
    if args.sample:
        step = max(1, len(codes) // args.sample)
        codes = codes[::step][:args.sample]

    frames = []
    used = 0
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date")
            close = pd.to_numeric(d["close"], errors="coerce")
            high = pd.to_numeric(d["high"], errors="coerce")
            amt = pd.to_numeric(d.get("amount", pd.Series(np.nan, index=d.index)),
                                errors="coerce")
            prev = close.shift(1)
            band = band_of(code, st_map.get(code, False))
            lim = (prev * (1 + band)).round(2)
            touch = (high >= lim - 1e-6) & prev.notna()
            seal = (close >= lim - 1e-6) & prev.notna()
            out = pd.DataFrame({
                "date": d["date"].values, "code": code,
                "close": close.values, "amt": amt.values,
                "touch": touch.values, "seal": seal.values,
                "pct": (close / prev - 1).values,
                "industry": ind_map.get(code, ""),
            })
            frames.append(out)
            used += 1
        except Exception:
            continue

    df = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    # 内存优化(全宇宙 ~1000 万行): 类别化 + 丢弃中间列
    df["code"] = df["code"].astype("category")
    df["industry"] = df["industry"].astype("category")
    df = df.drop(columns=["close"])
    # T+1 事件
    g = df.groupby("code", sort=False)
    df["touch_next"] = g["touch"].shift(-1)
    df["seal_next"] = g["seal"].shift(-1)
    # 近5日涨停次数(含当日)
    df["lu5"] = g["seal"].rolling(5, min_periods=1).sum().reset_index(level=0, drop=True)
    # 行业/市场当日涨停家数(本样本口径; 全宇宙时=市场)
    df["ind_lu"] = df.groupby(["date", "industry"])["seal"].transform("sum")
    df["mkt_lu"] = df.groupby("date")["seal"].transform("sum")
    # 排除: T日已涨停(红线) / 无次日样本
    base = df[(~df["seal"]) & df["touch_next"].notna()].copy()

    def rate(sub):
        n = len(sub)
        if n == 0:
            return {"n": 0, "p": None, "ci": [None, None]}
        p = float(sub["touch_next"].mean())
        lo, hi = wilson(p, n)
        return {"n": int(n), "p": p, "ci": [lo, hi]}

    res = {"mode": "full" if not args.sample else f"sample_{args.sample}",
           "stocks_used": used, "rows": int(len(base)),
           "unconditional": rate(base)}

    # 条件基率(锁定特征)
    cond = {}
    lu5_b = base["lu5"].clip(upper=2).astype(int)
    cond["lu5"] = {str(k): rate(base[lu5_b == k]) for k in sorted(lu5_b.unique())}
    pct_b = pd.cut(base["pct"], [0.02, 0.05, 0.09], right=False)
    cond["pct_t"] = {str(k): rate(base[pct_b == k]) for k in pct_b.cat.categories}
    # 炸板: T日触板但未封板
    cond["blowup_t"] = {"blowup": rate(base[base["touch"] & ~base["seal"]]),
                        "normal": rate(base[~base["touch"]])}
    # 分位: 成交额/行业涨停数/市场涨停数
    for col, name in [("amt", "amt_q"), ("ind_lu", "ind_lu_q"), ("mkt_lu", "mkt_lu_q")]:
        q = base.groupby("date")[col].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
        cond[name] = {str(k): rate(base[q == k]) for k in sorted(q.dropna().unique())}
    res["conditional"] = cond

    print(json.dumps(res, ensure_ascii=False, indent=1))
    out = f"logs/limitup_base_rates_{'sample' if args.sample else 'full'}.json"
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已保存 {out}")


if __name__ == "__main__":
    main()
