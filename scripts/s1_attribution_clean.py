"""
S1 干净版四臂归因 (2026-09-08 深夜, enable_stops 波及面扫描的量化件) —
修正 A1 组件污染: 原归因 A1("pool30买持")用 {} 覆盖=DEFAULT_CONFIG
全继承→MA10/TP/止损/拥挤度全开, "买持"名不副实; 池贡献 −2.68pp =
池差分+组件栈差分(A0全关 vs A1全开), 择时贡献 +1.38pp = 择时−组件
混合(A2组件关/A1组件开)。本脚本重建维度干净的臂:
  A0: pit800 全关(与 T3 同, PIT宇宙污染待成员表修复, 只保证臂间可比)
  A1c: pool30 买持, 组件全关(择时关+stops/MA10/TP/拥挤/过热全关)
  A2c: pool30 + 五档, 组件全关
  A3: 完整栈(=DEFAULT, 现网)
差分: A1c−A0=纯池贡献; A2c−A1c=纯择时; A3−A2c=纯组件(MA10+TP+拥挤,
stops在A2c/A3均开, 共同抵消)。
口径: 主窗口 2019-2026, 成本0.13%, 10路径摊平, 面板预加载+∩交易日历
(v3同口径)。OOS/抱团段暂不跑(OOS薄数据已标待复核; 抱团段待主窗结论)。
每臂记录 exit_ma10_sells(触发量断言, 0触发=标未验证)。
用法: python scripts/s1_attribution_clean.py > logs/s1_attribution_clean.log 2>&1
输出: logs/s1_attribution_clean.csv
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

START, END = WINDOWS["main"]
PATHS = 10
COMMISSION = float(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 0.0013

# 组件全关覆盖(纯池/纯择时臂): 在 A0_OV 的"全关"基础上只保留 pool30
COMPONENTS_OFF = {"enable_stops": False, "enable_ma10_exit": False,
                  "enable_take_profit": False, "max_vol20": 999.0,
                  "max_20d_return": 99999.0, "max_consec_up_days": 999,
                  "max_5d_return": 99999.0}
ARMS = [
    ("S1_A0_pit800全关",     A0_OV,                                False),
    ("S1_A1c_pool30买持纯",  {**A0_OV, **COMPONENTS_OFF,
                             "pool_style": "amount", "pool_size": 30,
                             "initial_capital": 500_000.0},         False),
    ("S1_A2c_pool30+五档纯", {**A0_OV, **COMPONENTS_OFF,
                             "pool_style": "amount", "pool_size": 30,
                             "initial_capital": 500_000.0},         True),
    ("S1_A3_完整栈现网",     {},                                   True),
]


def build_panels():
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
    sh = load_daily("SH000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, ap, cal


def main():
    pit = load_pit_memberships()
    entry_map = load_entry_map()
    panel, ap, cal = build_panels()
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
    all_dates = sorted({d for ps in paths for d in ps})
    pit_map = {d: sorted(members_on(d, pit, entry_map) or [])
               for d in all_dates}

    rows = []
    for arm_name, ov, with_timing in ARMS:
        anns, rets, tos, ma10_w = [], [], [], []
        for dates in paths:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    **ov, "commission": COMMISSION})
            nav, info = run_backtest(panel, ap, dates, cfg,
                                     ic if with_timing else None,
                                     pit_members=pit_map
                                     if ov.get("pool_style") == "pit800"
                                     else None)
            nav = nav[nav.index >= pd.Timestamp(START)]
            cm = calc_metrics(nav)
            anns.append(float(cm["年化_float"]))
            rets.append(nav.pct_change().dropna())
            tos.append(info["total_commission"] / COMMISSION / years)
            ma10_w.append(float(info.get("exit_ma10_sells", 0.0)))
        j = pd.concat(rets, axis=1).dropna()
        ens = (1 + j.mean(axis=1)).cumprod()
        ecm = calc_metrics(ens)
        vol_ann = float(j.mean(axis=1).std() * np.sqrt(252))
        flag = "✓" if np.mean(ma10_w) > 0 else "⚠️未验证"
        print(f"{arm_name}: 路径均值{np.mean(anns)*100:+.2f}% "
              f"摊平{ecm['年化_float']*100:+.2f}% 夏普{ecm['夏普_float']:.2f} "
              f"回撤{ecm['回撤_float']*100:.2f}% 换手{np.mean(tos)*100:.0f}%/年 "
              f"MA10退出{np.mean(ma10_w):.2f}{flag}", flush=True)
        rows.append({"arm": arm_name, "cost": COMMISSION,
                     "path_mean": round(float(np.mean(anns)), 6),
                     "ens_ann": round(float(ecm["年化_float"]), 6),
                     "sharpe": round(float(ecm["夏普_float"]), 4),
                     "dd": round(float(ecm["回撤_float"]), 6),
                     "vol_ann": round(vol_ann, 4),
                     "turnover": round(float(np.mean(tos)), 4),
                     "exit_ma10_sells": round(float(np.mean(ma10_w)), 4)})
        df = pd.DataFrame(rows)
        df.to_csv("logs/s1_attribution_clean.csv", index=False,
                  encoding="utf-8-sig")
    print("\n干净分解: 池=A1c−A0 | 择时=A2c−A1c | 组件=A3−A2c")
    print("落盘: logs/s1_attribution_clean.csv")


if __name__ == "__main__":
    main()
