"""H-BREADTH-1/2 预登记检验（2019-2026；IS 2019-2023 / OOS 2024-2026）。

唯一主定义：T 日 SH 收涨 且 全 A 广度中位数 < 0（div）。
主指标：OOS 的 next_ret(div) − next_ret(non)；判据见
`logs/research_registration_breadth_20261028.md`。
只跑一次；不搜索阈值/定义。不生成仓位信号。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PANEL = ROOT / "logs" / "breadth_panel_2019_2026.parquet"
OUT_JSON = ROOT / "logs" / "breadth_divergence_20261028.json"
OUT_MD = ROOT / "logs" / "breadth_divergence_20261028.md"


def load_sh():
    frames = []
    for yr in range(2019, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000001.parquet")
        if p.exists():
            d = pd.read_parquet(p)
            d["date"] = pd.to_datetime(d["date"])
            frames.append(d[["date", "close"]])
    d = pd.concat(frames).drop_duplicates("date").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    d["ret_t"] = d.close.pct_change() * 100
    d["vol20"] = d.ret_t.rolling(20).std()
    d["ma200_dist"] = (d.close / d.close.rolling(200).mean() - 1) * 100
    d["next_ret"] = d.ret_t.shift(-1)
    d["next_abs"] = d.next_ret.abs()
    return d.reset_index()


def stats(x, y):
    x, y = pd.Series(x).dropna(), pd.Series(y).dropna()
    if len(x) < 2 or len(y) < 2:
        return {"n1": len(x), "n2": len(y), "diff": None, "mde": None,
                "t": None}
    sd = np.sqrt(((len(x) - 1) * x.var() + (len(y) - 1) * y.var())
                 / (len(x) + len(y) - 2))
    diff = float(x.mean() - y.mean())
    mde = float(2.8 * sd * np.sqrt(1 / len(x) + 1 / len(y)))
    se = np.sqrt(x.var() / len(x) + y.var() / len(y))
    return {"n1": int(len(x)), "n2": int(len(y)),
            "mean1": round(float(x.mean()), 3),
            "mean2": round(float(y.mean()), 3),
            "diff": round(diff, 3), "mde": round(mde, 3),
            "t": round(float(diff / se), 2) if se else None}


def run(df, label):
    d = df.dropna(subset=["next_ret", "ret_t", "vol20", "ma200_dist",
                          "median_pct"]).copy()
    d["div"] = ((d.ret_t > 0) & (d.median_pct < 0)).astype(int)
    a, b = d[d["div"] == 1], d[d["div"] == 0]
    out = {"label": label, "n": int(len(d)), "n_div": int(len(a)),
           "n_non": int(len(b)),
           "next_ret": stats(a.next_ret, b.next_ret),
           "next_abs": stats(a.next_abs, b.next_abs)}
    for target in ("next_ret", "next_abs"):
        try:
            m = smf.ols(f"{target} ~ div + ret_t + vol20 + ma200_dist",
                        data=d).fit(cov_type="HAC",
                                    cov_kwds={"maxlags": 5})
            out[f"ols_{target}"] = {
                "coef_div": round(float(m.params["div"]), 3),
                "t_div": round(float(m.tvalues["div"]), 2),
                "n": int(m.nobs), "r2": round(float(m.rsquared), 4)}
        except Exception as e:  # noqa: BLE001
            out[f"ols_{target}"] = {"error": str(e)[:80]}
    return out


def main():
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel.date)
    sh = load_sh()
    df = sh.merge(panel, on="date", how="inner").sort_values("date")
    is_df = df[df.date <= "2023-12-31"]
    oos = df[df.date >= "2024-01-01"]
    res = {"data_asof": str(df.date.max().date()),
           "n_days": int(len(df)), "is": run(is_df, "IS_2019_2023"),
           "oos": run(oos, "OOS_2024_2026")}
    # 预登记判据
    p = res["oos"]["next_ret"]
    o = res["is"]["next_ret"]
    ols = res["oos"].get("ols_next_ret", {})
    cond = {
        "oos_diff_negative": p["diff"] is not None and p["diff"] < 0,
        "oos_abs_ge_mde": (p["diff"] is not None and p["mde"] is not None
                           and abs(p["diff"]) >= p["mde"]),
        "is_same_sign": (o["diff"] is not None and p["diff"] is not None
                         and o["diff"] < 0 and p["diff"] < 0),
        "ols_t_ge_2": (ols.get("t_div") is not None
                       and ols["t_div"] <= -2.0),
        "n_oos_div_ge_30": res["oos"]["n_div"] >= 30,
        "n_all_div_ge_80": res["is"]["n_div"] + res["oos"]["n_div"] >= 80,
    }
    passed = all(cond.values())
    res["criteria"] = cond
    res["verdict"] = "PASS_FORWARD_PAPER" if passed else "FAIL_CLOSED"
    # 10-26/27 样本外轶事（不参与拟合）
    res["forward_anecdote"] = {
        "date": "2026-10-26", "sh_pct": 0.19, "breadth_median": -0.46,
        "up_pct": 40.4, "next_ret_2026-10-27": -1.33,
        "note": "N=1；仅记录，不作为证据"}
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = ["# H-BREADTH 检验（2019-2026；预登记）", "",
             f"- 数据末端: {res['data_asof']}；n={res['n_days']} 日；唯一背离定义。",
             f"- IS div={res['is']['n_div']} / non={res['is']['n_non']}；"
             f"OOS div={res['oos']['n_div']} / non={res['oos']['n_non']}",
             f"- **判定: {res['verdict']}**", "",
             "| 样本 | next_ret div | next_ret non | diff (MDE) | t | next_abs diff (MDE) |",
             "|---|---:|---:|---:|---:|---:|"]
    for k in ("is", "oos"):
        r = res[k]
        lines.append(
            f"| {r['label']} | {r['next_ret'].get('mean1')} | "
            f"{r['next_ret'].get('mean2')} | "
            f"{r['next_ret'].get('diff')} ({r['next_ret'].get('mde')}) | "
            f"{r['next_ret'].get('t')} | "
            f"{r['next_abs'].get('diff')} ({r['next_abs'].get('mde')}) |")
    lines += ["", "## OLS 稳健性（HAC maxlags=5）", ""]
    for k in ("is", "oos"):
        lines.append(f"- {res[k]['label']}: next_ret div coef="
                     f"{res[k]['ols_next_ret'].get('coef_div')} "
                     f"t={res[k]['ols_next_ret'].get('t_div')}; "
                     f"next_abs div coef={res[k]['ols_next_abs'].get('coef_div')} "
                     f"t={res[k]['ols_next_abs'].get('t_div')}")
    lines += ["", "## 预登记判据", ""]
    for k, v in cond.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## 10-26/27 样本外轶事", "",
              "10-26 上证 +0.19%、中位 −0.46%、上涨 40.4% → 10-27 −1.33%。"
              "N=1，仅记录，不作为证据。", "",
              "## 盲区", "",
              "- 事后假设；2019-2026 regime 混杂；广度含 ST/停牌/新股；",
              "- 日频重叠/自相关仅 HAC 部分修正；只测 SH；OOS 已用一次。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(json.dumps(res, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
