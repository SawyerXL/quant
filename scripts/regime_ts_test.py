"""H2/H3 单变量检验（预注册 logs/prereg_regime_20261023.md，N=74/75）。

固定口径、只跑一次：
- H2: m1_m2_gap_lag1 → sh_fwd_6m；五分位；IS 2008-02~2018-12，OOS 2019-01~2025-10；
  HAC(lag=6)；MDE 23.0pp（iid 11.1pp）；方向=正。
- H3: sf_equity_12m_lag1 → sh_fwd_6m_mdd；三分位；IS 2016-01~2018-12，
  OOS 2019-01~2025-10；HAC(lag=6)；MDE 9.4pp（iid 4.5pp）；方向=负
  （高供给→回撤更深）。
判据：PASS = 同号 + IS |t|≥2.5 + OOS 同号 |t|≥1.5 + |OOS差|≥MDE；
FAIL = 符号反转或 |OOS差|<iid MDE；其余 SUGGESTIVE/UNDERPOWERED。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).parent.parent
PANEL = ROOT / "data_store" / "regime" / "panel_monthly.parquet"
CFG = {
    "H2": dict(n=74, pred="m1_m2_gap_lag1", target="sh_fwd_6m",
               is_s="2008-02", is_e="2018-12", oos_s="2019-01", oos_e="2025-10",
               k=5, lag=6, mde=0.230, mde_iid=0.111, sign=+1),
    "H3": dict(n=75, pred="sf_equity_12m_lag1", target="sh_fwd_6m_mdd",
               is_s="2016-01", is_e="2018-12", oos_s="2019-01", oos_e="2025-10",
               k=3, lag=6, mde=0.094, mde_iid=0.045, sign=-1),
}


def _fit(df, thresholds, pred, target, k, lag):
    d = df.dropna(subset=[pred, target]).copy()
    d["bin"] = np.digitize(d[pred], thresholds, right=False)
    hi, lo = k - 1, 0
    d = d[d["bin"].isin([hi, lo])]
    if d.empty or d["bin"].nunique() < 2:
        return None
    X = sm.add_constant(pd.DataFrame(
        {f"q{hi}": (d["bin"] == hi).astype(float),
         f"q{lo}": (d["bin"] == lo).astype(float)}, index=d.index))
    m = sm.OLS(d[target], X).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    b_hi, b_lo = float(m.params[f"q{hi}"]), float(m.params[f"q{lo}"])
    v = m.cov_params()
    se = float(np.sqrt(v.loc[f"q{hi}", f"q{hi}"] + v.loc[f"q{lo}", f"q{lo}"]
                       - 2 * v.loc[f"q{hi}", f"q{lo}"]))
    return {"diff": b_hi - b_lo, "t": (b_hi - b_lo) / se if se > 0 else np.nan,
            "mean_hi": float(d.loc[d["bin"] == hi, target].mean()),
            "mean_lo": float(d.loc[d["bin"] == lo, target].mean()),
            "n_hi": int((d["bin"] == hi).sum()),
            "n_lo": int((d["bin"] == lo).sum()), "n": len(d), "se": se}


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "H2"
    c = CFG[key]
    d = pd.read_parquet(PANEL)
    d["month"] = pd.to_datetime(d["month"]); d = d.set_index("month").sort_index()
    is_df, oos_df = d.loc[c["is_s"]:c["is_e"]], d.loc[c["oos_s"]:c["oos_e"]]
    qs = [i / c["k"] for i in range(1, c["k"])]
    th = np.quantile(is_df[c["pred"]].dropna(), qs)
    r_is = _fit(is_df, th, c["pred"], c["target"], c["k"], c["lag"])
    r_oos = _fit(oos_df, th, c["pred"], c["target"], c["k"], c["lag"])
    if r_is is None or r_oos is None:
        print("样本不足"); return 1
    same = (np.sign(r_is["diff"]) == c["sign"]
            and np.sign(r_oos["diff"]) == c["sign"])
    if (not same) or abs(r_oos["diff"]) < c["mde_iid"]:
        verdict = "FAIL"
    elif (abs(r_is["t"]) >= 2.5 and abs(r_oos["t"]) >= 1.5
          and abs(r_oos["diff"]) >= c["mde"]):
        verdict = "PASS"
    elif same:
        verdict = "SUGGESTIVE"
    else:
        verdict = "UNDERPOWERED"
    out = {"n": c["n"], "hypothesis": key, "prereg": "logs/prereg_regime_20261023.md",
           "pred": c["pred"], "target": c["target"],
           "is": [str(is_df.index.min())[:10], str(is_df.index.max())[:10]],
           "oos": [str(oos_df.index.min())[:10], str(oos_df.index.max())[:10]],
           "thresholds": [round(float(x), 4) for x in th],
           "mde": c["mde"], "mde_iid": c["mde_iid"], "sign": c["sign"],
           "hac_lag": c["lag"], "is_result": r_is, "oos_result": r_oos,
           "verdict": verdict, "as_of": datetime.now().isoformat(timespec="seconds")}
    out_path = ROOT / "logs" / f"regime_{key.lower()}_20261023.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
