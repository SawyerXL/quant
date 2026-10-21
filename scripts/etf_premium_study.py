"""QDII 溢价研究（2026-10-20）：513100 / 159941 / 513500。

数据：
- NAV 历史：ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
- 二级市场价：ak.fund_etf_hist_sina(symbol=sh/sz+code)
计算 premium = price/NAV - 1；分析分布、当前水平、溢价变动对价格收益的贡献、
以及“高溢价不买”规则的历史效果。
用法：python scripts/etf_premium_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

META = Path("data_store/meta")
ETFS = {"513100": "sh513100", "159941": "sz159941", "513500": "sh513500"}
OUT = Path("logs/etf_premium_20261021.json")


def adjust_series(s: pd.Series, thresh: float = -0.15):
    """份额折算/拆分检测：单日 < thresh 视为拆分，收益置 0 后重算净值。

    2026-10-21 实锤：513100 2022-01-14 −80.45%、159941 2022-07-05
    （NAV 2022-07-04）−74.66%、513500 2022-03-30 −49.18% 均为未复权
    份额折算；原始收益/年化被污染。真实拆分日市场收益≈0，单日误差对
    年化 <0.05pp；不推断拆分比例。
    返回 (调整后收益, 调整后水平, 拆分日列表)。
    """
    r = s.pct_change()
    splits = r[r < thresh]
    ra = r.copy()
    ra.loc[splits.index] = 0.0
    adj = (1 + ra.fillna(0.0)).cumprod() * float(s.iloc[0])
    return ra, adj, [str(d.date()) for d in splits.index]


def load_nav(code):
    d = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    d = d.copy()
    d["date"] = pd.to_datetime(d["净值日期"])
    d["nav"] = pd.to_numeric(d["单位净值"], errors="coerce")
    return d.set_index("date")["nav"].dropna().sort_index()


def load_px(sym):
    d = ak.fund_etf_hist_sina(symbol=sym)
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index("date")["close"].astype(float).sort_index()


def main():
    out = {"as_of": "2026-10-20", "etfs": {}}
    allprem = {}
    for code, sym in ETFS.items():
        try:
            nav = load_nav(code)
            px = load_px(sym)
            df = pd.concat([px.rename("px"), nav.rename("nav")], axis=1, join="inner").dropna()
            if df.empty:
                print(code, "empty join"); continue
            df["prem"] = df["px"] / df["nav"] - 1
            df["nav_ret"] = df["nav"].pct_change()
            df["px_ret"] = df["px"].pct_change()
            nav_ra, nav_adj, nav_splits = adjust_series(df["nav"])
            px_ra, px_adj, px_splits = adjust_series(df["px"])
            df["nav_ret_adj"] = nav_ra
            df["px_ret_adj"] = px_ra
            df["px_adj"] = px_adj
            split_days = sorted(set(nav_splits) | set(px_splits))
            # 拆分日（NAV/PX 可能错一天）溢价无意义，置 NaN
            df.loc[df.index.isin(pd.to_datetime(split_days)), "prem"] = np.nan
            allprem[code] = df["prem"]
            df.to_parquet(META / f"etf_premium_{code}.parquet")
            p = df["prem"].dropna()
            cur = p.iloc[-1]
            out["etfs"][code] = {
                "n": int(len(df)), "start": str(df.index.min().date()),
                "end": str(df.index.max().date()),
                "prem_now_pct": round(float(cur) * 100, 2),
                "prem_mean_pct": round(float(p.mean()) * 100, 2),
                "prem_median_pct": round(float(p.median()) * 100, 2),
                "prem_p10_pct": round(float(p.quantile(0.10)) * 100, 2),
                "prem_p90_pct": round(float(p.quantile(0.90)) * 100, 2),
                "prem_max_pct": round(float(p.max()) * 100, 2),
                "prem_min_pct": round(float(p.min()) * 100, 2),
                "prem_2019_now_change_pp": round(
                    float((p[p.index >= "2019-01-01"].iloc[-1]
                           - p[p.index >= "2019-01-01"].iloc[0]) * 100), 2),
                "px_ann_adj_pct": round(float(
                    (px_adj.iloc[-1] / px_adj.iloc[0]) ** (252 / len(df)) - 1) * 100, 2),
                "nav_ann_adj_pct": round(float(
                    (nav_adj.iloc[-1] / nav_adj.iloc[0]) ** (252 / len(df)) - 1) * 100, 2),
                "split_days": split_days,
            }
            # 溢价分位 × 未来20日价格收益（诊断，用拆分校准价）
            df["fwd20"] = df["px_adj"].shift(-20) / df["px_adj"] - 1
            df["q"] = pd.qcut(df["prem"], 5, labels=False, duplicates="drop")
            q = df.groupby("q").agg(prem_mean=("prem", "mean"),
                                    fwd20_mean=("fwd20", "mean"),
                                    n=("prem", "size"))
            out["etfs"][code]["prem_quintile_fwd20_pct"] = {
                int(i): {"prem_mean_pct": round(float(r.prem_mean) * 100, 2),
                         "fwd20_mean_pct": round(float(r.fwd20_mean) * 100, 2),
                         "n": int(r.n)} for i, r in q.iterrows()}
            print(code, out["etfs"][code], flush=True)
        except Exception as e:
            print(code, "ERR", str(e)[:200], flush=True)
    if allprem:
        pr = pd.DataFrame(allprem).dropna(how="all")
        pr.to_parquet(META / "etf_premium_panel.parquet")
        # 当前三只对比
        out["current"] = {c: round(float(pr[c].dropna().iloc[-1]) * 100, 2)
                          for c in pr.columns if pr[c].notna().any()}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
