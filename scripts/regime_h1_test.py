"""H1（N=73）：估值（ERP 分位）→ 未来 12 个月收益。

按 `logs/prereg_regime_20261023.md` 锁定口径：
- IS 2010-04~2018-12 计算五分位阈值；OOS 2019-01~2025-10 沿用 IS 阈值。
- Q5（高 ERP=便宜）− Q1（低 ERP=贵）的 12M 前向收益均值差。
- OLS（q4/q0 哑变量）HAC(Newey-West, lag=12) t。
- 判据：PASS=IS差>0且IS t≥2.5且OOS差>0且OOS t≥1.5且OOS差≥39.0pp；
  SUGGESTIVE=同号且OOS差14.8~39.0pp或t未达；FAIL=符号反转或|差|<14.8pp；
  其余 UNDERPOWERED。**只跑一次，不调窗口/阈值。**
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
OUT = ROOT / "logs" / "regime_h1_20261023.json"
PRED = "erp_pct_10y"
TARGET = "sh_fwd_12m"
IS = ("2010-04", "2018-12")
OOS = ("2019-01", "2025-10")
HAC_LAG = 12
MDE = 0.390          # 预注册保守 MDE（39.0pp）
MDE_IID = 0.148      # 参考
THRESH = None        # 由 IS 计算


def _fit(df, thresholds):
    d = df.dropna(subset=[PRED, TARGET]).copy()
    d["bin"] = np.digitize(d[PRED], thresholds, right=False)
    d = d[d["bin"].isin([0, 4])]
    if d.empty or d["bin"].nunique() < 2:
        return None
    X = sm.add_constant(pd.DataFrame({"q4": (d["bin"] == 4).astype(float),
                                      "q0": (d["bin"] == 0).astype(float)},
                                     index=d.index))
    m = sm.OLS(d[TARGET], X).fit(cov_type="HAC",
                                 cov_kwds={"maxlags": HAC_LAG})
    b4, b0 = float(m.params["q4"]), float(m.params["q0"])
    v = m.cov_params()
    se = float(np.sqrt(v.loc["q4", "q4"] + v.loc["q0", "q0"]
                       - 2 * v.loc["q4", "q0"]))
    n4 = int((d["bin"] == 4).sum()); n0 = int((d["bin"] == 0).sum())
    return {"diff": b4 - b0, "t": (b4 - b0) / se if se > 0 else np.nan,
            "mean_q5": float(d.loc[d["bin"] == 4, TARGET].mean()),
            "mean_q1": float(d.loc[d["bin"] == 0, TARGET].mean()),
            "n_q5": n4, "n_q1": n0, "n": len(d), "se": se}


def main():
    d = pd.read_parquet(PANEL)
    d["month"] = pd.to_datetime(d["month"]); d = d.set_index("month").sort_index()
    is_df = d.loc[IS[0]:IS[1]]
    oos_df = d.loc[OOS[0]:OOS[1]]
    th = np.quantile(is_df[PRED].dropna(), [0.2, 0.4, 0.6, 0.8])
    r_is = _fit(is_df, th)
    r_oos = _fit(oos_df, th)
    if r_is is None or r_oos is None:
        print("样本不足: IS/OOS 某一组为空")
        return 1
    if r_is["diff"] <= 0 or r_oos["diff"] <= 0 or abs(r_oos["diff"]) < MDE_IID:
        verdict = "FAIL"
    elif (r_is["diff"] > 0 and r_is["t"] >= 2.5 and r_oos["diff"] > 0
          and r_oos["t"] >= 1.5 and r_oos["diff"] >= MDE):
        verdict = "PASS"
    elif r_is["diff"] > 0 and r_oos["diff"] > 0:
        verdict = "SUGGESTIVE"
    else:
        verdict = "UNDERPOWERED"
    out = {"n": 73, "hypothesis": "H1 ERP pct → 12M forward",
           "prereg": "logs/prereg_regime_20261023.md",
           "is": [str(is_df.index.min())[:10], str(is_df.index.max())[:10]],
           "oos": [str(oos_df.index.min())[:10], str(oos_df.index.max())[:10]],
           "thresholds": [round(float(x), 4) for x in th],
           "mde": MDE, "mde_iid": MDE_IID, "hac_lag": HAC_LAG,
           "is_result": r_is, "oos_result": r_oos, "verdict": verdict,
           "as_of": datetime.now().isoformat(timespec="seconds")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
