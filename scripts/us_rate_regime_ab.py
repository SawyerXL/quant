"""美元潮汐可检验部分：美债10Y利率状态 vs 上证前瞻收益（描述性 A/B）。

口径：
- 美债10Y：akshare `bond_zh_us_rate` 的“美国国债收益率10年”，日频→月末；
- 上证：本地 SH000001 日线→月末；
- 状态：10Y 12个月变化 >0=上行，<=0=下行；10Y>=4%=高位，<3%=低位；
  10Y 高于10个月均线=above_ma；
- 目标：月末后 1/3/6/12 个月上证收益（1m非重叠；3/6/12重叠，仅描述）；
- MDE=2.8×pooled_sd×sqrt(1/n1+1/n2)（近似，双侧95%）；
- 只做描述，不生成信号；OOS 未消耗。

用法: python scripts/us_rate_regime_ab.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "logs" / "us_rate_regime_20261028.json"
OUT_MD = ROOT / "logs" / "us_rate_regime_20261028.md"
CACHE = ROOT / "logs" / "us10y_daily_20261027.csv"
CASH_ANNUAL = 0.02  # 现金/货基保守年化假设


def load_sh_monthly():
    frames = []
    for yr in range(2000, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000001.parquet")
        if p.exists():
            d = pd.read_parquet(p)
            d["date"] = pd.to_datetime(d["date"])
            frames.append(d[["date", "close"]])
    d = pd.concat(frames).drop_duplicates("date").sort_values("date")
    d = d.set_index("date")["close"].astype(float)
    return d.resample("ME").last().dropna()


def load_us10y():
    if CACHE.exists():
        d = pd.read_csv(CACHE)
    else:
        import akshare as ak
        d = ak.bond_zh_us_rate(start_date="20000101")
        d.to_csv(CACHE, index=False, encoding="utf-8")
    d["date"] = pd.to_datetime(d["日期"])
    d = d[["date", "美国国债收益率10年"]].dropna()
    d.columns = ["date", "y10"]
    d = d.set_index("date")["y10"].astype(float)
    return d.resample("ME").last().dropna()


def stats(x):
    x = pd.Series(x).dropna()
    if len(x) == 0:
        return {"n": 0}
    return {"n": int(len(x)), "mean": round(float(x.mean()) * 100, 2),
            "median": round(float(x.median()) * 100, 2),
            "win": round(float((x > 0).mean() * 100), 1),
            "min": round(float(x.min()) * 100, 2),
            "max": round(float(x.max()) * 100, 2)}


def mde(a, b):
    a, b = pd.Series(a).dropna(), pd.Series(b).dropna()
    if len(a) < 2 or len(b) < 2:
        return None
    sd = np.sqrt(((len(a) - 1) * a.var() + (len(b) - 1) * b.var())
                 / (len(a) + len(b) - 2))
    return round(float(2.8 * sd * np.sqrt(1 / len(a) + 1 / len(b)) * 100), 2)


def main():
    sh = load_sh_monthly()
    y10 = load_us10y()
    df = pd.concat({"sh": sh, "y10": y10}, axis=1).dropna()
    df["chg12"] = df.y10 - df.y10.shift(12)
    df["ma10"] = df.y10.rolling(10).mean()
    df["rising"] = df.chg12 > 0
    df["above_ma"] = df.y10 > df.ma10
    df["high4"] = df.y10 >= 4.0
    df["low3"] = df.y10 < 3.0
    for h in (1, 3, 6, 12):
        df[f"fwd{h}"] = df.sh.shift(-h) / df.sh - 1

    out = {"as_of": str(df.index[-1].date()), "start": str(df.index[0].date()),
           "n_months": int(len(df)), "regimes": {}}
    for name, mask in (("rising", df.rising), ("falling", ~df.rising),
                       ("above_ma", df.above_ma),
                       ("below_ma", ~df.above_ma),
                       ("high4", df.high4), ("low3", df.low3)):
        out["regimes"][name] = {}
        for h in (1, 3, 6, 12):
            st = stats(df.loc[mask, f"fwd{h}"])
            cash_h = ((1 + CASH_ANNUAL) ** (h / 12) - 1) * 100
            st["cash_pct"] = round(cash_h, 2)
            st["excess_vs_cash"] = (round(st["mean"] - cash_h, 2)
                                    if st.get("mean") is not None else None)
            out["regimes"][name][f"fwd{h}m"] = st
    out["diffs"] = {}
    masks = {"rising": df.rising, "falling": ~df.rising,
             "above_ma": df.above_ma, "below_ma": ~df.above_ma,
             "high4": df.high4, "low3": df.low3}
    for a, b in (("rising", "falling"), ("above_ma", "below_ma"),
                 ("high4", "low3")):
        out["diffs"][f"{a}_minus_{b}"] = {}
        for h in (1, 3, 6, 12):
            x = df.loc[masks[a], f"fwd{h}"]
            y = df.loc[masks[b], f"fwd{h}"]
            out["diffs"][f"{a}_minus_{b}"][f"fwd{h}m"] = {
                "mean_diff": round(float(x.mean() - y.mean()) * 100, 2)
                if len(x) and len(y) else None,
                "mde": mde(x, y)}
    # 2010+ 子样本
    d2 = df[df.index >= "2010-01-01"]
    out["post2010"] = {}
    for name, mask in (("rising", d2.rising), ("falling", ~d2.rising)):
        out["post2010"][name] = {}
        for h in (1, 3, 6, 12):
            st = stats(d2.loc[mask, f"fwd{h}"])
            cash_h = ((1 + CASH_ANNUAL) ** (h / 12) - 1) * 100
            st["cash_pct"] = round(cash_h, 2)
            st["excess_vs_cash"] = (round(st["mean"] - cash_h, 2)
                                    if st.get("mean") is not None else None)
            out["post2010"][name][f"fwd{h}m"] = st
    # 降息拐点：12m变化由正转负
    piv = df[(df.chg12.shift(1) > 0) & (df.chg12 <= 0)]
    out["easing_pivots"] = []
    for dt, row in piv.iterrows():
        out["easing_pivots"].append({
            "date": str(dt.date()), "y10": round(float(row.y10), 2),
            "fwd3": round(float(row.fwd3) * 100, 2) if pd.notna(row.fwd3) else None,
            "fwd6": round(float(row.fwd6) * 100, 2) if pd.notna(row.fwd6) else None,
            "fwd12": round(float(row.fwd12) * 100, 2) if pd.notna(row.fwd12) else None,
        })
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    lines = ["# 美债10Y状态 vs 上证前瞻收益（2000-2026，描述性）", "",
             f"- as_of={out['as_of']}，n={out['n_months']} 个月；1m非重叠，3/6/12m重叠。",
             "- 状态为月末已知值；不生成信号。", "",
             "## 状态均值（%）", "",
             "| 状态 | n | fwd1m | fwd3m | fwd6m | fwd12m | 1m胜率 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, r in out["regimes"].items():
        lines.append(f"| {name} | {r['fwd1m']['n']} | {r['fwd1m'].get('mean','')} | "
                     f"{r['fwd3m'].get('mean','')} | {r['fwd6m'].get('mean','')} | "
                     f"{r['fwd12m'].get('mean','')} | {r['fwd1m'].get('win','')}% |")
    lines += ["", "## 差异与MDE（%）", "",
              "| 对比 | fwd1m | fwd3m | fwd6m | fwd12m |",
              "|---|---:|---:|---:|---:|"]
    for name, r in out["diffs"].items():
        lines.append(f"| {name} | " + " | ".join(
            f"{r[f'fwd{h}m']['mean_diff']} (MDE {r[f'fwd{h}m']['mde']})"
            for h in (1, 3, 6, 12)) + " |")
    lines += ["", "## 相对现金（现金年化 2%）", "",
              "| 状态 | 1m超额 | 3m超额 | 6m超额 | 12m超额 |",
              "|---|---:|---:|---:|---:|"]
    for name, r in out["regimes"].items():
        lines.append(f"| {name} | " + " | ".join(
            str(r[f'fwd{h}m'].get('excess_vs_cash')) for h in (1, 3, 6, 12)) + " |")
    lines += ["", "## 2010+ 子样本", ""]
    for name, r in out["post2010"].items():
        lines.append(f"- {name}: n={r['fwd1m']['n']}，1m均值 {r['fwd1m'].get('mean')}%，"
                     f"3m {r['fwd3m'].get('mean')}%，6m {r['fwd6m'].get('mean')}%")
    lines += ["", "## 降息拐点（12m变化由正转负）", ""]
    for p in out["easing_pivots"]:
        lines.append(f"- {p['date']}: y10={p['y10']}，fwd3={p['fwd3']}%，"
                     f"fwd6={p['fwd6']}%，fwd12={p['fwd12']}%")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1)[:5000])


if __name__ == "__main__":
    main()
