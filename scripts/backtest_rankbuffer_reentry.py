"""
rank buffer + MA10重入冷却 A/B（2026-09-04，待办#17，文献"容忍带调仓"映射）。

采纳标准(事前锁定, spec §7-17): 双窗口+双成本档收益不劣+换手降幅>15%,
网格须平台。两项独立测不叠加。
- rank buffer: 进30/出(30×mult), mult网格{1.25, 1.5, 2.0}
- MA10重入冷却: N∈{5,10,20}交易日禁重买
- 窗口: 全期2019-2026.8 + OOS 2015-2018(250bar预热吃掉2015年, 已知限制)
- 成本档: 0.13% / 0.30% 双边
- 口径: 当前部署配置(pool30 × 50万/组 lot约束, vol5%, MA10-4d)
换手口径: 单边年换手 = (总佣金/费率)/年数; 降幅 = 1 - 变体/基线。
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

START, END = "2019-01-01", "2026-08-28"
BASE_CFG = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}


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


def run_one(panel, ap, rebal, idx_c, commission, **overrides):
    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE_CFG,
                            "commission": commission, **overrides})
    nav, info = run_backtest(panel, ap, rebal, cfg, idx_c)
    cm = calc_metrics(nav)
    ar = float(cm["年化_float"])
    dd = float(cm["回撤_float"])
    years = (panel.index[-1] - panel.index[0]).days / 365.25
    turnover = (info["total_commission"] / commission) / years
    return ar, dd, turnover


def main():
    sh = load_daily("000001", "2014-06-01", "2026-08-28")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    windows = [("全期2019-26.8", "2019-01-01", "2026-08-28"),
               ("OOS2015-18", "2015-01-01", "2018-12-31")]
    panels = {wn: load_window(lo, hi, cal) for wn, lo, hi in windows}

    rows = []
    for wn, lo, hi in windows:
        panel, ap, idx_c, rebal = panels[wn]
        for comm in (0.0013, 0.0030):
            base_ar, base_dd, base_to = run_one(panel, ap, rebal, idx_c, comm)
            rows.append({"窗口": wn, "成本": f"{comm:.2%}", "变体": "基线",
                         "年化": base_ar * 100, "回撤": base_dd * 100,
                         "换手": base_to * 100, "换手降幅": 0.0})
            # rank buffer 网格
            for mult in (1.25, 1.5, 2.0):
                ar, dd, to = run_one(panel, ap, rebal, idx_c, comm,
                                     rank_buffer_mult=mult)
                rows.append({"窗口": wn, "成本": f"{comm:.2%}",
                             "变体": f"rank{mult:.2f}", "年化": ar * 100,
                             "回撤": dd * 100, "换手": to * 100,
                             "换手降幅": (1 - to / base_to) * 100})
            # MA10 重入冷却
            for cool in (5, 10, 20):
                ar, dd, to = run_one(panel, ap, rebal, idx_c, comm,
                                     ma10_reentry_cool=cool)
                rows.append({"窗口": wn, "成本": f"{comm:.2%}",
                             "变体": f"cool{cool}", "年化": ar * 100,
                             "回撤": dd * 100, "换手": to * 100,
                             "换手降幅": (1 - to / base_to) * 100})

    df = pd.DataFrame(rows)
    df.to_csv("logs/rankbuffer_reentry_ab.csv", index=False)
    print(df.round(2).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
