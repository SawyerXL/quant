"""ZigZag 描述性研究(2026-10-19 R2 第一阶段, 不出交易结论)。

样本: 从 stock_info_full 等距抽取 100 只(剔除 ST), 2019-01-01~2026-09-30。
输出: pivot 确认滞后分布(P50/P90)、波段幅度/长度分布(按年份)、
      每年 pivot 数与 20 日波动率/成交额的相关性。
用法: python scripts/zigzag_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from zigzag import zigzag_pivots

THRESH = 0.05
START, END = "2019-01-01", "2026-09-30"
SAMPLE_N = 100


def main():
    meta = load_meta("stock_info_full")
    meta = meta[~meta["is_st"].astype(bool)] if "is_st" in meta else meta
    codes = sorted(meta["code"].astype(str).tolist())
    step = max(1, len(codes) // SAMPLE_N)
    sample = codes[::step][:SAMPLE_N]

    lags, swings = [], []          # swings: (year, amplitude, bars)
    per_year = {}
    used = 0
    stock_metrics = []             # 每股因子: pivot密度/波动/动量/成交额/行业
    ind_map = dict(zip(meta["code"].astype(str),
                       meta.get("industry_l1", pd.Series("", index=meta.index))))
    for code in sample:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date").set_index("date")
            close = pd.to_numeric(d["close"], errors="coerce").dropna()
            if len(close) < 250:
                continue
            piv = zigzag_pivots(close.values, THRESH)
            used += 1
            for x in piv:
                lags.append(x.lag)
            for a, b in zip(piv, piv[1:]):
                if a.pivot_idx < b.pivot_idx:
                    amp = abs(b.price / a.price - 1)
                    bars = b.pivot_idx - a.pivot_idx
                    year = close.index[a.pivot_idx].year
                    swings.append((year, amp, bars))
                    per_year.setdefault(year, []).append(amp)
            # 年度 pivot 密度 vs 波动
            y_counts = {}
            for x in piv:
                y = close.index[x.confirm_idx].year
                y_counts[y] = y_counts.get(y, 0) + 1
            for y, cnt in y_counts.items():
                per_year.setdefault(y, [])
            # 每股因子(描述性相关用)
            ret = close.pct_change().dropna()
            amt = pd.to_numeric(d["amount"], errors="coerce") if "amount" in d else pd.Series(dtype=float)
            stock_metrics.append({
                "code": code,
                "pivot_density": len(piv) / (len(close) / 250.0),   # 每年 pivot 数
                "lag_median": float(np.median([x.lag for x in piv])) if piv else None,
                "amp_median": float(np.median([abs(b.price / a.price - 1)
                                               for a, b in zip(piv, piv[1:])])) if len(piv) > 1 else None,
                "vol_ann": float(ret.std() * np.sqrt(252)),
                "ret_ann": float((close.iloc[-1] / close.iloc[0]) ** (252 / len(close)) - 1),
                "avg_amount": float(amt.mean()) if len(amt) else None,
                "industry": ind_map.get(code, ""),
            })
        except Exception:
            continue

    lags = np.array(lags)
    swings = pd.DataFrame(swings, columns=["year", "amp", "bars"])
    out = {
        "threshold": THRESH,
        "sample_requested": SAMPLE_N,
        "sample_used": used,
        "pivots_total": int(len(lags)),
        "lag_bars": {
            "P50": float(np.percentile(lags, 50)) if len(lags) else None,
            "P90": float(np.percentile(lags, 90)) if len(lags) else None,
            "max": int(lags.max()) if len(lags) else None,
        },
        "swing_amplitude": {
            "P50": float(swings["amp"].quantile(0.5)) if len(swings) else None,
            "P90": float(swings["amp"].quantile(0.9)) if len(swings) else None,
        },
        "swing_bars": {
            "P50": float(swings["bars"].quantile(0.5)) if len(swings) else None,
            "P90": float(swings["bars"].quantile(0.9)) if len(swings) else None,
        },
        "by_year": {
            int(y): {"n": len(g), "amp_median": float(g["amp"].median()),
                     "bars_median": float(g["bars"].median())}
            for y, g in swings.groupby("year")
        },
        "note": "描述性研究; pivot 使用必须以 confirm_idx 为可用时点.",
    }

    # ── ③ 与已有因子的相关性(交付物要求; 描述性, 非信号) ──
    sm = pd.DataFrame(stock_metrics).dropna(subset=["pivot_density", "vol_ann"])
    if len(sm) >= 20:
        sm["log_amount"] = np.log(sm["avg_amount"].clip(lower=1))
        corr = {}
        for col in ("vol_ann", "ret_ann", "log_amount"):
            corr[col] = float(sm["pivot_density"].corr(sm[col], method="spearman"))
        # 波动率控制后的残差相关(看 swing 结构是否另有信息)
        try:
            import numpy as _np
            X = _np.column_stack([_np.ones(len(sm)), sm["vol_ann"].values])
            beta, *_ = _np.linalg.lstsq(X, sm["pivot_density"].values, rcond=None)
            resid = sm["pivot_density"].values - X @ beta
            resid_corr = {c: float(pd.Series(resid).corr(sm[c].reset_index(drop=True),
                                                         method="spearman"))
                          for c in ("ret_ann", "log_amount")}
        except Exception:
            resid_corr = {}
        out["factor_corr_spearman"] = corr
        out["factor_corr_resid_vs_vol"] = resid_corr
        out["n_stocks_for_corr"] = int(len(sm))
        # 行业分布(样本内)
        ind_stats = {}
        for ind, g in sm.groupby("industry"):
            if len(g) >= 3:
                ind_stats[ind] = {"n": int(len(g)),
                                  "pivot_density_median": float(g["pivot_density"].median()),
                                  "amp_median": float(g["amp_median"].median())
                                  if g["amp_median"].notna().any() else None}
        out["by_industry"] = ind_stats
    print(json.dumps(out, ensure_ascii=False, indent=1))
    Path("logs/zigzag_study_20261019.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
