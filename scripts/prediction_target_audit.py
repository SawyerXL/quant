"""预测目标审计：上证（当前台账）vs 中证800（股票腿交易宇宙）。

只做描述性对账：同一天两个指数的方向一致率、上证上涨日中证800下跌的
比例，以及把现有台账的 p_up 同时套到两个目标上的 Brier 反事实。
不改台账、不改策略；用途是回答“预测上证是否等于预测我们交易的股票”。

运行: python scripts/prediction_target_audit.py
产出: logs/prediction_target_audit_20261026.json
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.market_daily_study import _load_index

OUT = ROOT / "logs" / "prediction_target_audit_20261026.json"
LEDGER = ROOT / "logs" / "prediction_ledger.csv"


def _merged(start="2019-01-01"):
    sh = _load_index("SH000001")[["date", "close"]].rename(columns={"close": "sh"})
    cb = _load_index("SH000906")[["date", "close"]].rename(columns={"close": "cb"})
    m = pd.merge(sh, cb, on="date", how="inner").sort_values("date")
    m = m[m["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    m["sh_ret"] = m["sh"].pct_change()
    m["cb_ret"] = m["cb"].pct_change()
    m = m.dropna(subset=["sh_ret", "cb_ret"]).reset_index(drop=True)
    m["sh_up"] = (m["sh_ret"] > 0).astype(int)
    m["cb_up"] = (m["cb_ret"] > 0).astype(int)
    return m


def _direction_stats(m):
    n = len(m)
    agree = int((m["sh_up"] == m["cb_up"]).sum())
    sh_up = m[m["sh_up"] == 1]
    sh_dn = m[m["sh_up"] == 0]
    return {
        "n_days": n,
        "direction_agree_pct": round(agree / n * 100, 2),
        "direction_disagree_pct": round((1 - agree / n) * 100, 2),
        "corr": round(float(m["sh_ret"].corr(m["cb_ret"])), 3),
        "p_cb_down_given_sh_up_pct": round(float((sh_up["cb_up"] == 0).mean()) * 100, 2),
        "p_cb_up_given_sh_down_pct": round(float((sh_dn["cb_up"] == 1).mean()) * 100, 2),
        "n_sh_up": int(len(sh_up)),
        "n_sh_down": int(len(sh_dn)),
        "mean_cb_ret_on_sh_up_pct": round(float(sh_up["cb_ret"].mean()) * 100, 3),
        "mean_cb_ret_on_sh_down_pct": round(float(sh_dn["cb_ret"].mean()) * 100, 3),
    }


def _ledger_counterfactual(m):
    if not LEDGER.exists():
        return {"error": "ledger missing"}
    df = pd.read_csv(LEDGER)
    v = df[(df["valid"] == "ok") & df["outcome_pct"].notna()].copy()
    v = v.sort_values("registered_at").drop_duplicates(
        ["market_date", "horizon_d"], keep="last"
    )
    rows = []
    for _, r in v.iterrows():
        day = pd.Timestamp(r["market_date"])
        pos = m.index[m["date"] == day]
        h = int(r["horizon_d"])
        if len(pos) == 0:
            continue
        pos = int(pos[0])
        base = pos - 1
        end = pos + h - 1
        if base < 0 or end >= len(m):
            continue
        # 与 prediction_ledger 同口径：t-1 收盘 → t+h-1 收盘
        sh_ret = float(m.iloc[end]["sh"] / m.iloc[base]["sh"] - 1)
        cb_ret = float(m.iloc[end]["cb"] / m.iloc[base]["cb"] - 1)
        p = float(r["p_up"])
        rows.append({
            "market_date": str(day.date()),
            "horizon_d": h,
            "p_up": p,
            "sh_ret_pct": round(sh_ret * 100, 3),
            "cb_ret_pct": round(cb_ret * 100, 3),
            "sh_up": int(sh_ret > 0),
            "cb_up": int(cb_ret > 0),
        })
    if not rows:
        return {"error": "no matched scored rows"}
    x = pd.DataFrame(rows)
    out = {"n": len(x), "rows": rows}
    for target in ("sh", "cb"):
        y = x[f"{target}_up"]
        out[target] = {
            "brier": round(float(((x["p_up"] - y) ** 2).mean()), 4),
            "hit": int((x["p_up"].apply(lambda p: 1 if p >= 0.5 else 0) == y).sum()),
            "n": len(x),
        }
    return out


def main():
    m = _merged()
    out = {
        "asof": str(m["date"].max().date()),
        "start": str(m["date"].min().date()),
        "same_day": _direction_stats(m),
        "ledger_counterfactual": _ledger_counterfactual(m),
        "notes": [
            "同一天方向一致率衡量‘上证能否代理中证800’，不是预测模型检验。",
            "台账反事实是把同一 p_up 套到两个目标；p_up 原本只对上证登记，"
            "中证800列仅用于测量目标错位，不作为新预测。",
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("->", OUT)


if __name__ == "__main__":
    main()
