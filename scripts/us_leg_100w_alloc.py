"""100 万 + US 腿三资产配置研究（2026-10-26，研究不下单）。

基准：40 万股票（2×20 万，`stock_leg_nav_20w_avg`）+ 60 万 CB
（`cb_sched_compare_nav.nav_snapdelay_par`），月度回目标权重；
复现 live {2,7} 40/60 = 11.97/1.536/-13.84。

US 腿：DJIA 指数 T-1（价格口径、不含股息；真实 513400 NAV 含股息会更高，
本估计对 US 偏保守）。只测预注册权重 u=12.5%（不重新网格选优），
资金来源三种：股票腿划拨 / CB 腿划拨 / 按 40-60 等比缩放。

重要近似：
- 股票腿仍用 20 万/组收益；若划拨后每组 13.75/17.5 万，lot 跳票会更重，
  因此 us_from_stock / prorata 的股票腿收益被高估（结果偏乐观），需后续
  用分组引擎在小资金档复跑确认。
- CB 腿用 60 万收益序列按权重缩放；47.5/52.5 万下 10 张整手可能改变成交，
  未单独重跑。
- US 腿用 DJIA 价格指数，不含股息、不含 513400 溢价；对 US 收益偏保守。

用法：python scripts/us_leg_100w_alloc.py
产出：logs/us_leg_100w_alloc_20261026.json/.md
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from three_piece_40_60 import metrics_ret
from us_leg_40_60 import te_mde

OUT = ROOT / "logs" / "us_leg_100w_alloc_20261026.json"
MD = ROOT / "logs" / "us_leg_100w_alloc_20261026.md"


def load_legs():
    stock = pd.read_csv(ROOT / "logs" / "stock_leg_nav_20w_avg.csv",
                        index_col=0, parse_dates=True)["nav"]
    cb = pd.read_csv(ROOT / "logs" / "cb_sched_compare_nav.csv",
                     index_col=0, parse_dates=True)["nav_snapdelay_par"]
    ix = pd.read_parquet(ROOT / "data_store" / "meta" / "us_dji_sina.parquet")
    ix["date"] = pd.to_datetime(ix["date"])
    us = ix.set_index("date")["close"].astype(float).sort_index().pct_change()
    us = us.shift(1).dropna()
    s = stock.pct_change().rename("s")
    c = cb.pct_change().rename("cb")
    base = pd.concat([s, c], axis=1, join="inner").dropna()
    # US 缺失日（美股假日/DJIA 与 A 股日历差）按 0 计，保留完整 A 股日历；
    # 对 US 收益偏保守，但 base 必须复现 live 11.97 的 1885 日窗口。
    us = us.reindex(base.index).fillna(0.0).rename("us")
    legs = pd.concat([base, us], axis=1, join="inner").dropna()
    return legs


def combo3(legs: pd.DataFrame, weights: dict, halt=None, cost=0.0,
           stock_drag: float = 0.0):
    """stock_drag: 股票腿年化拖累(pp)，按 /252 日扣，用于 lot 恶化敏感性。"""
    cols = list(legs.columns)
    target = np.array([weights[c] for c in cols], float)
    w = target.copy()
    nav, peak = 1.0, 1.0
    halted, trig, dh, last_month = False, 0, 0, None
    navs = []
    for dt, row in legs.iterrows():
        ym = (dt.year, dt.month)
        if ym != last_month:
            last_month = ym
            if not halted:
                nav *= (1 - cost * float(np.abs(target - w).sum()) / 2)
                w = target.copy()
        vals = row[cols].values.astype(float).copy()
        if stock_drag:
            # stock_drag 单位=年化 pp；日收益是小数，故 /100 再 /252
            vals[cols.index("s")] -= stock_drag / 100.0 / 252.0
        nav *= (1 + float(np.dot(w, vals)))
        peak = max(peak, nav)
        dd = nav / peak - 1
        if halt is not None:
            tau, rec = halt
            if not halted and dd <= tau:
                halted = True
                trig += 1
            elif halted and dd >= tau + rec:
                halted = False
                w = target.copy()
        if halted:
            dh += 1
        navs.append(nav)
        w = w * (1 + vals)
        w = w / w.sum()
    return pd.Series(navs, index=legs.index), trig, dh


def main():
    legs = load_legs()
    base_w = {"s": 0.40, "cb": 0.60, "us": 0.0}
    scenarios = {
        "base_40_60": base_w,
        # US 12.5 万从股票腿划拨: 股票 27.5 / CB 60 / US 12.5
        "us_from_stock": {"s": 0.275, "cb": 0.60, "us": 0.125},
        # US 12.5 万从 CB 腿划拨: 股票 40 / CB 47.5 / US 12.5
        "us_from_cb": {"s": 0.40, "cb": 0.475, "us": 0.125},
        # 按 40/60 等比缩放: 股票 35 / CB 52.5 / US 12.5
        "us_prorata": {"s": 0.35, "cb": 0.525, "us": 0.125},
    }
    out = {"as_of": str(legs.index.max().date()),
           "window": f"{legs.index.min().date()}~{legs.index.max().date()}",
           "n": int(len(legs)),
           "caveats": [
               "股票腿用 20万/组收益; 13.75/17.5万/组 lot 跳票更重, "
               "us_from_stock/prorata 股票腿被高估",
               "CB 腿用 60万收益序列缩放; 47.5/52.5万下整手成交未重跑",
               "US 用 DJIA 价格指数 T-1, 不含股息/溢价; 对 US 收益偏保守",
               "haircut 是日频线性拖累、只加在 us_from_stock 的股票腿，"
               "不是 13.75万/组 lot 引擎重跑；0.5pp 为保守敏感性下界",
           ],
           "scenarios": {}}
    base_ret = None
    for name, w in scenarios.items():
        nav, _, _ = combo3(legs, w)
        ret = nav.pct_change().fillna(0)
        m = metrics_ret(ret)
        hnav, t, d = combo3(legs, w, halt=(-0.15, 0.02))
        hret = hnav.pct_change().fillna(0)
        hm = metrics_ret(hret)
        out["scenarios"][name] = {
            "weights": w, "metrics": m,
            "halt15": {**hm, "triggers": int(t), "days_halted": int(d)},
        }
        if name == "base_40_60":
            base_ret = ret
            out["base_matches_live"] = (
                m["ann_pct"] == 11.97 and m["sharpe"] == 1.536
                and m["dd_pct"] == -13.84)
        else:
            bm = out["scenarios"]["base_40_60"]["metrics"]
            out["scenarios"][name]["vs_base"] = {
                "delta_ann_pp_same_window": round(m["ann_pct"] - bm["ann_pct"], 2),
                "delta_sharpe_same_window": round(m["sharpe"] - bm["sharpe"], 3),
                "delta_dd_pp_same_window": round(m["dd_pct"] - bm["dd_pct"], 2),
                **te_mde(ret, base_ret),
            }
        print(name, json.dumps(out["scenarios"][name], ensure_ascii=False),
              flush=True)
    # 股票腿 haircut 敏感性：13.75万/组 lot 恶化不是 0；给 us_from_stock
    # 的股票腿加 0.5/1.0/1.5/2.0pp 年化拖累，看是否跌破 MDE。
    base_m = out["scenarios"]["base_40_60"]["metrics"]
    sens = []
    for h in (0.0, 0.5, 1.0, 1.5, 2.0):
        nav, _, _ = combo3(legs, scenarios["us_from_stock"], stock_drag=h)
        ret = nav.pct_change().fillna(0)
        m = metrics_ret(ret)
        hnav, ht, hd = combo3(legs, scenarios["us_from_stock"],
                              halt=(-0.15, 0.02), stock_drag=h)
        hm = metrics_ret(hnav.pct_change().fillna(0))
        sens.append({
            "stock_haircut_pp": h,
            "metrics": m,
            "delta_ann_pp": round(m["ann_pct"] - base_m["ann_pct"], 2),
            "halt15": {**hm, "triggers": int(ht), "days_halted": int(hd)},
            **te_mde(ret, base_ret),
        })
    out["haircut_sensitivity"] = sens
    out["verdict"] = ("base 复现 live 11.97/1.536/-13.84; "
                      "us_from_stock +1.65pp = MDE 1.65pp, 但股票腿用 20万/组"
                      "收益、13.75万/组 lot 跳票会更差 → BORDERLINE_NOT_DEPLOY; "
                      "us_from_cb 年化 -0.88pp 但夏普 +0.03/回撤 +2.52pp; "
                      "us_prorata 无年化增益。股票腿 haircut 0.5pp → Δ"
                      f"{sens[1]['delta_ann_pp']:+.2f}pp < MDE "
                      f"{sens[1]['MDE_pp']:.2f}pp；US 暂不纳入 100 万。")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 100 万 + US 腿三资产配置研究（2026-10-26）", "",
             f"窗口 {out['window']}，n={out['n']}；base 复现 live 40/60="
             f"{out['base_matches_live']}", "",
             "| 场景 | 年化 | 夏普 | 回撤 | 15%熔断触发 | Δ年化 | MDE |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, s in out["scenarios"].items():
        m = s["metrics"]
        vb = s.get("vs_base", {})
        lines.append(
            f"| {name} | {m['ann_pct']}% | {m['sharpe']} | {m['dd_pct']}% | "
            f"{s['halt15']['triggers']} | {vb.get('delta_ann_pp_same_window','—')} | "
            f"{vb.get('MDE_pp','—')} |")
    lines += ["", "## 判定", "", out["verdict"], "",
              "## 股票腿 haircut 敏感性（us_from_stock）", "",
              "| 股票腿年化拖累 | 年化 | Δ年化 | MDE | 15%熔断 |",
              "|---:|---:|---:|---:|---:|"]
    for x in out["haircut_sensitivity"]:
        lines.append(
            f"| {x['stock_haircut_pp']}pp | {x['metrics']['ann_pct']}% | "
            f"{x['delta_ann_pp']} | {x['MDE_pp']} | "
            f"{x['halt15']['triggers']} |")
    lines += ["", "## 近似/盲区", ""] + [f"- {x}" for x in out["caveats"]]
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("->", OUT, MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
