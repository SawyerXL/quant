"""
T8 新臂立项开跑 (2026-09-08 overnight, 用户条件批准) —
新臂: 800等权(PIT) + 五档择时 + MA10-4d + 拥挤度过滤(max_vol20=5), 不含TP。
对照组: T3统一A0(800等权退化臂, 2000万); 若T5判定"隐性降杠杆"则对照组
形态改为"同beta的800等权"(本脚本支持 --control beta_adjusted, 未实装前
先输出两版增量)。
口径: 双窗口(main 2019-2026 / OOS 2015-2018) × 双成本(0.13%/0.30%)
× 10路径摊平; 2019-2021 抱团段单独输出; 预加载面板(与v3同口径,
面板∩交易日历)。
判读(事前钉死): 组件增量 = 新臂 − 对照组; < +1.0pp → 组件alpha与池特性
绑定、不具通用性, §6.8"组件是真alpha"表述二次降级。净差三项同表:
成本节省 + 股息税(+0.3pp) + 打新损失(T6数值)。
用法: python scripts/t8_new_arm.py
输出: logs/t8_new_arm_results.csv
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

# 新臂配置: 800等权退化臂 + 五档开 + MA10-4d开 + 拥挤度5% + TP关
NEW_OV = {**A0_OV, "enable_ma10_exit": True, "max_vol20": 5.0}
PATHS = 10
COMMISSIONS = [0.0013, 0.0030]  # 双成本档
SEGMENTS = {"main": WINDOWS["main"], "oos": WINDOWS["oos"],
            "main_2019_2021": ("2019-01-01", "2021-12-31")}


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
    # 日历必须从 LOAD_START 起, 否则面板∩日历删掉OOS预加载段
    # (v3首轮教训: OOS臂端暖机退回2015-06)
    sh = load_daily("000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, ap, cal


def run_arm(panel, ap, ic, dates, cfg, pit_map=None, with_timing=True):
    ic_arg = ic if with_timing else None
    nav, info = run_backtest(panel, ap, dates, cfg, ic_arg,
                             pit_members=pit_map)
    return nav, info


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
            for arm_name, ov, with_timing in [("对照_800等权", A0_OV, False),
                                              ("新臂_800+五档+MA10+拥挤", NEW_OV, True)]:
                anns, rets, tos = [], [], []
                for dates in paths:
                    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                            **ov, "commission": comm})
                    nav, info = run_arm(panel, ap, ic, dates, cfg, pit_map,
                                        with_timing)
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
    df.to_csv("logs/t8_new_arm_results.csv", index=False, encoding="utf-8-sig")
    print("\n结果落盘: logs/t8_new_arm_results.csv")


if __name__ == "__main__":
    main()
