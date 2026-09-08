"""
S0 组件隔离回测 (2026-09-08 晚间, 用户§0再分解要求) —
把T8新臂(800等权+五档+MA10-4d+拥挤度vol20=5)拆成增量链:
  S0_800+五档            = A0_OV + 择时开(组件全关)
  S0_800+五档+MA10       = 上一级 + MA10-4d开
  S0_800+五档+拥挤       = 上一级 + vol20=5开
  S0_800+五档+MA10+拥挤  = T8全量新臂(重跑, 与T8 CSV做复现校验)
目的: 检验§0解释(b)——组件的价值是否主要来自MA10或过滤, 各自在800池上
的独立增量是多少。T5真alpha(p=0.000)与T8全<+1.0pp的矛盾, 若增量链显示
"拥挤度独立增量≈0而MA10大亏"则组件价值在池30绑定于过滤+池形态, 不在MA10。
口径: 与T8完全一致(双成本0.13/0.30 × 10路径摊平; 面板∩交易日历; PIT成员
主窗口/抱团段; 不含OOS——用户裁定OOS薄数据无意义, 回填后再补)。
判读(事前钉死):
  拥挤独立增量 = (S0_800+五档+拥挤) − (S0_800+五档)
  MA10独立增量  = (S0_800+五档+MA10) − (S0_800+五档)
  若拥挤独立增量 ≥ +1.0pp → 过滤器迁移性成立, T8判读需反转(结合§0a触发率)
  若拥挤独立增量 < +1.0pp 且 §0a触发率<3% → 解释(a): 800池无可剔的坏票
  若MA10独立增量 << 0 → 解释(b): 全量组合的亏损主力是MA10
用法: python scripts/s0_isolation.py > logs/s0_isolation_run.log 2>&1
输出: logs/s0_isolation_results.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import (WINDOWS, BASE, A0_OV, load_blacklist,
                                         load_pit_memberships, load_entry_map,
                                         members_on)

PATHS = 10
COMMISSIONS = [0.0013, 0.0030]
SEGMENTS = {"main": WINDOWS["main"],
            "main_2019_2021": ("2019-01-01", "2021-12-31")}
ARMS = [
    ("S0_800+五档",            {**A0_OV}),
    ("S0_800+五档+MA10",       {**A0_OV, "enable_ma10_exit": True}),
    ("S0_800+五档+拥挤",       {**A0_OV, "max_vol20": 5.0}),
    ("S0_800+五档+MA10+拥挤",  {**A0_OV, "enable_ma10_exit": True, "max_vol20": 5.0}),
]


def build_panels(START, END):
    LOAD_START = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, LOAD_START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
                if len(amt) >= 200:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    # 日历从 LOAD_START 起(否则面板∩日历删掉OOS预加载段, v3教训)
    sh = load_daily("000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, ap, cal


def main():
    pit = load_pit_memberships()
    entry_map = load_entry_map()
    rows = []
    for seg_name, (START, END) in SEGMENTS.items():
        print(f"\n{'='*70}\n段 {seg_name} ({START} ~ {END})\n{'='*70}", flush=True)
        panel, ap, cal = build_panels(START, END)
        ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
        ic.index = pd.to_datetime(ic.index)
        base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
        idx = {d: i for i, d in enumerate(cal)}

        def path(off):
            shifted = [cal[idx.get(d, 0) + off] for d in base
                       if idx.get(d, 0) + off < len(cal)]
            return [d for d in shifted if START <= d <= END]

        paths = [path(off) for off in range(PATHS)]
        years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
        use_pit = pit if seg_name.startswith("main") else None
        all_dates = sorted({d for ps in paths for d in ps})
        pit_map = {d: sorted(members_on(d, use_pit, entry_map) or [])
                   for d in all_dates}

        for comm in COMMISSIONS:
            for arm_name, ov in ARMS:
                anns, rets, tos = [], [], []
                for dates in paths:
                    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                            **ov, "commission": comm})
                    nav, info = run_backtest(panel, ap, dates, cfg, ic,
                                             pit_members=pit_map)
                    nav = nav[nav.index >= pd.Timestamp(START)]
                    cm = calc_metrics(nav)
                    anns.append(float(cm["年化_float"]))
                    rets.append(nav.pct_change().dropna())
                    tos.append(info["total_commission"] / comm / years)
                j = pd.concat(rets, axis=1).dropna()
                ens = (1 + j.mean(axis=1)).cumprod()
                ecm = calc_metrics(ens)
                print(f"{arm_name} 成本{comm*100:.2f}%: 路径均值"
                      f"{np.mean(anns)*100:+.2f}% 摊平{ecm['年化_float']*100:+.2f}% "
                      f"夏普{ecm['夏普_float']:.2f} 回撤{ecm['回撤_float']*100:.2f}% "
                      f"换手{np.mean(tos)*100:.0f}%/年", flush=True)
                rows.append({"segment": seg_name, "cost": comm, "arm": arm_name,
                             "path_mean": round(float(np.mean(anns)), 6),
                             "ens_ann": round(float(ecm["年化_float"]), 6),
                             "sharpe": round(float(ecm["夏普_float"]), 4),
                             "dd": round(float(ecm["回撤_float"]), 6),
                             "turnover": round(float(np.mean(tos)), 4)})
        df = pd.DataFrame(rows)
        df.to_csv("logs/s0_isolation_results.csv", index=False, encoding="utf-8-sig")
    print("\n结果落盘: logs/s0_isolation_results.csv")


if __name__ == "__main__":
    main()
