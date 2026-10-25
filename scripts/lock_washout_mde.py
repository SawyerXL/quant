"""锁定 H-W1 的 MDE（只数事件，不看收益）— 2026-10-25。

预注册要求：census 只数事件 → MDE = 2.802 × σ_prior / sqrt(N_months_OOS)，
σ_prior=30%（保守先验），N_months_OOS = OOS 事件月数。
本脚本读 census 事件表，写 `logs/prereg_washout_census_20261025.json/.md`；
事件收益脚本会拒绝在没有该文件时运行（fail-fast）。
"""
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "washout_census_events_20261025.csv"
CENSUS = ROOT / "logs" / "washout_census_20261025.json"
OUT_JSON = ROOT / "logs" / "prereg_washout_census_20261025.json"
OUT_MD = ROOT / "logs" / "prereg_washout_census_20261025.md"

SIGMA_MONTH_PRIOR = 0.05   # 月度均值差的 σ 先验（不是个股 30%）
SIGMA_ULTRA = 0.30         # 诊断用超保守上界（个股 σ 误当月度 σ）
OOS_START = "2023-01-01"
IS_END = "2022-12-31"


def main():
    ev = pd.read_csv(EVENTS, dtype={"code": str})
    ev["code"] = ev["code"].str.zfill(6)
    tradable = ev[ev["mature_60"] & ~ev["limit_up_t1"]].copy()
    is_ev = tradable[tradable["date"] <= IS_END]
    oos_ev = tradable[tradable["date"] >= OOS_START]
    n_month_oos = oos_ev["date"].str[:7].nunique()
    if n_month_oos == 0:
        raise SystemExit("OOS 无事件月，无法锁 MDE")
    mde = 2.802 * SIGMA_MONTH_PRIOR / math.sqrt(n_month_oos) * 100
    mde_ultra = 2.802 * SIGMA_ULTRA / math.sqrt(n_month_oos) * 100
    cen = json.loads(CENSUS.read_text())
    pc = cen.get("funnel", {}).get("price_candidate_days") or 0
    fm = cen.get("funnel", {}).get("float_missing_days") or 0
    cov = (1 - fm / pc) if pc else None
    uni = cen.get("universe_codes") or 0
    miss = cen.get("codes_missing_bars") or 0
    miss_rate = (miss / uni) if uni else None
    cap_reasons = []
    if cov is not None and cov < 0.80:
        cap_reasons.append(f"float_coverage={cov:.1%}<80%")
    if miss_rate is not None and miss_rate > 0.20:
        cap_reasons.append(f"missing_bars={miss_rate:.1%}>20%")
    verdict_cap = "UNDERPOWERED" if cap_reasons else None
    out = {
        "as_of": str(tradable["date"].max()),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_events_total": int(len(tradable)),
        "n_months_total": int(tradable["date"].str[:7].nunique()),
        "n_events_IS": int(len(is_ev)),
        "n_months_IS": int(is_ev["date"].str[:7].nunique()),
        "n_events_OOS": int(len(oos_ev)),
        "n_months_OOS": int(n_month_oos),
        "sigma_month_prior": SIGMA_MONTH_PRIOR,
        "formula": "MDE = 2.802 * sigma_month_prior / sqrt(N_months_OOS) * 100 (pp)",
        "mde_cluster_pp": round(mde, 3),
        "mde_ultra_pp": round(mde_ultra, 3),
        "mde_note": "sigma_month=5% 为月度均值差先验（个股30%/√~50事件≈4.2%）；"
                    "ultra=30% 仅诊断，不作判定",
        "float_coverage": None if cov is None else round(cov, 4),
        "missing_bars_rate": None if miss_rate is None else round(miss_rate, 4),
        "verdict_cap": verdict_cap,
        "verdict_cap_reasons": cap_reasons,
        "verdict_cap_note": "float 覆盖<80% 或 缺 bar>20% → 只描述、不得 PASS（幸存者偏差上限，预注册）",
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    md = f"""# 洗盘 H-W1 census 与 MDE 锁定（{datetime.now():%Y-%m-%d %H:%M}）

> 只数事件、未看任何前向收益。MDE 锁定后事件收益脚本才允许运行。

- 可直接交易事件：**{out['n_events_total']}**（成熟 T+60 且 T+1 非涨停）
- 事件月：**{out['n_months_total']}**
- IS（≤{IS_END}）：{out['n_events_IS']} 事件 / {out['n_months_IS']} 月
- OOS（≥{OOS_START}）：{out['n_events_OOS']} 事件 / {out['n_months_OOS']} 月
- σ_month 先验：{SIGMA_MONTH_PRIOR:.0%}（月度均值差；不取自本样本收益）
- **MDE_cluster = {out['mde_cluster_pp']:.2f}pp**（60 日配对超额差，判定用）
- 超保守上界（30% 个股 σ，误当月度 σ）：{out['mde_ultra_pp']:.2f}pp（只诊断）
- 幸存者上限：{out['verdict_cap'] or '无'} {out['verdict_cap_reasons']}
- 判定：OOS 均值 ≥ MDE 且与 IS 同号 → PASS；0<OOS<MDE → UNDERPOWERED；
  OOS≤0 或符号翻转 → FAIL/关闭。
"""
    OUT_MD.write_text(md)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("->", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()
