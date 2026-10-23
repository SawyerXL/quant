"""H1 稳健性诊断（预注册中列为 secondary，不计独立检验）。

只回答：ERP 分位的结果是否只是构造方式造成的？用同一 IS 阈值、同一 OOS：
- 主口径 erp_pct_10y（高=便宜，预期正）
- 对照 pe_pct_10y（高=贵，预期负）
- 对照原始 erp（高=便宜，预期正）
不改 H1 判定、不重跑主检验；仅记录方向与量级。
"""
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).parent.parent
PANEL = ROOT / "data_store" / "regime" / "panel_monthly.parquet"
OUT = ROOT / "logs" / "regime_h1_robust_20261023.json"
TARGET = "sh_fwd_12m"
IS = ("2010-04", "2018-12")
OOS = ("2019-01", "2025-10")


def _fit(df, pred, thresholds, lag=12):
    d = df.dropna(subset=[pred, TARGET]).copy()
    d["bin"] = np.digitize(d[pred], thresholds, right=False)
    d = d[d["bin"].isin([0, 4])]
    if d.empty or d["bin"].nunique() < 2:
        return None
    X = sm.add_constant(pd.DataFrame(
        {"q4": (d["bin"] == 4).astype(float),
         "q0": (d["bin"] == 0).astype(float)}, index=d.index))
    m = sm.OLS(d[TARGET], X).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    b4, b0 = float(m.params["q4"]), float(m.params["q0"])
    v = m.cov_params()
    se = float(np.sqrt(v.loc["q4", "q4"] + v.loc["q0", "q0"]
                       - 2 * v.loc["q4", "q0"]))
    return {"diff": b4 - b0, "t": (b4 - b0) / se if se > 0 else None,
            "n_q5": int((d["bin"] == 4).sum()),
            "n_q1": int((d["bin"] == 0).sum()),
            "mean_q5": float(d.loc[d["bin"] == 4, TARGET].mean()),
            "mean_q1": float(d.loc[d["bin"] == 0, TARGET].mean())}


def main():
    d = pd.read_parquet(PANEL)
    d["month"] = pd.to_datetime(d["month"]); d = d.set_index("month").sort_index()
    is_df, oos_df = d.loc[IS[0]:IS[1]], d.loc[OOS[0]:OOS[1]]
    out = {"note": "secondary robustness only; H1 verdict unchanged",
           "as_of": datetime.now().isoformat(timespec="seconds"), "results": {}}
    for pred, expect in [("erp_pct_10y", "+"), ("pe_pct_10y", "-"),
                         ("erp", "+")]:
        th = np.quantile(is_df[pred].dropna(), [0.2, 0.4, 0.6, 0.8])
        r_is = _fit(is_df, pred, th)
        r_oos = _fit(oos_df, pred, th)
        out["results"][pred] = {"expect": expect,
                                "is": r_is, "oos": r_oos,
                                "thresholds": [round(float(x), 4) for x in th]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
