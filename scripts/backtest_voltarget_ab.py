"""
目标波动率控仓 A/B（2026-09-04，结构层研究1，三根桩已事前登记）。

桩①成功标准: 非收益赢五档——目标波动率参数在网格内平台+对波动率
  估计窗口(20/60日)不敏感, 范式简化本身就是收益, 收益持平即胜。
桩②已知风险: 急跌顺周期(波动率↑→仓位↓→低位轻仓)——仓位下限
  20~30%大概率仍需保留; 本脚本含 floor 0.2 敏感性对照(算不算第二个
  参数的判决材料)。
桩③对照组: 五档基线 + "五档+更早降档"(ma200_thresh_shift 敏感性
  方向), 目标波动率要赢的是修补后的五档, 不是原始版。

口径: pool30 × 50万/组 lot约束(部署口径); 窗口全期+OOS。
vol_target>0时引擎以 clip(target/指数实现波动率, floor, 1) 替代五档,
  每日更新、调仓日生效(与引擎架构一致)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

BASE = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}
WINDOWS = [("全期2019-26.8", "2019-01-01", "2026-08-28"),
           ("OOS2015-18", "2015-01-01", "2018-12-31")]
VARIANTS = [
    ("五档基线", {}),
    ("五档-更早降档2%", {"ma200_thresh_shift": -0.02}),
    ("五档-更早降档3%", {"ma200_thresh_shift": -0.03}),
    ("vol10%×20d", {"vol_target": 0.10, "vol_window": 20, "vol_floor_pos": 0.3}),
    ("vol10%×60d", {"vol_target": 0.10, "vol_window": 60, "vol_floor_pos": 0.3}),
    ("vol12%×20d", {"vol_target": 0.12, "vol_window": 20, "vol_floor_pos": 0.3}),
    ("vol12%×60d", {"vol_target": 0.12, "vol_window": 60, "vol_floor_pos": 0.3}),
    ("vol15%×20d", {"vol_target": 0.15, "vol_window": 20, "vol_floor_pos": 0.3}),
    ("vol15%×60d", {"vol_target": 0.15, "vol_window": 60, "vol_floor_pos": 0.3}),
    ("vol12%×20d×floor0.2", {"vol_target": 0.12, "vol_window": 20,
                             "vol_floor_pos": 0.2}),
]


def load_window(lo, hi, cal):
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, lo, hi)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx_c = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
    return panel, ap, idx_c, rebal


def main():
    sh = load_daily("000001", "2014-06-01", "2026-08-28")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rows = []
    for wn, lo, hi in WINDOWS:
        panel, ap, idx_c, rebal = load_window(lo, hi, cal)
        print(f"\n===== {wn} =====", flush=True)
        for name, ov in VARIANTS:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **ov})
            nav, _ = run_backtest(panel, ap, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            rows.append({"窗口": wn, "变体": name,
                         "年化": cm["年化_float"] * 100,
                         "夏普": cm["夏普_float"],
                         "回撤": cm["回撤_float"] * 100,
                         "波动": cm["波动_float"] * 100})
            print(f"{name:<18} 年化{cm['年化_float']*100:+.2f}% "
                  f"夏普{cm['夏普_float']:.2f} 回撤{cm['回撤_float']*100:.2f}% "
                  f"波动{cm['波动_float']*100:.1f}%", flush=True)
    pd.DataFrame(rows).to_csv("logs/voltarget_ab.csv", index=False)


if __name__ == "__main__":
    main()
