"""
熔断方案 × 阈值网格稳健性验证 (2026-08-30)。

动机: 单阈值(-25%)每窗口仅触发1次, 属于案例路径。阈值网格把触发次数
扩到几十次(半独立试验), 检验A/B/C方案排序是否跨阈值稳定。
- 阈值: -10/-15/-20/-25%
- 方案: none对照 / A全清仓 / B暂停开仓 / C降30%底仓
- 窗口: 全期2019-2026.8 + OOS 2015-2018
输出: 每个(阈值,方案)的年化/夏普/回撤/触发次数, 排序稳定性一眼可读。
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

MODES = [("none", "无熔断"), ("A", "A全清仓"), ("B", "B暂停开仓"), ("C", "C降30%")]
THRESHOLDS = [0.10, 0.15, 0.20, 0.25]


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
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
    return panel, ap, idx_c, rebal


def main():
    sh = load_daily("000001", "2014-06-01", "2026-08-28")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    windows = [("全期2019-2026.8", "2019-01-01", "2026-08-28"),
               ("OOS 2015-2018", "2015-01-01", "2018-12-31")]

    for wname, lo, hi in windows:
        print(f"\n{'='*88}\n窗口 {wname}\n{'='*88}", flush=True)
        panel, ap, idx_c, rebal = load_window(lo, hi, cal)
        print(f"  面板 {panel.shape[0]}天×{panel.shape[1]}只\n", flush=True)
        print(f"{'阈值':>8}{'方案':<12}{'年化':>9}{'夏普':>8}{'最大回撤':>10}{'触发':>6}", flush=True)
        for th in THRESHOLDS:
            for mode, label in MODES:
                cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(),
                                        "halt_mode": mode, "halt_dd_limit": th})
                nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
                cm = calc_metrics(nav)
                ar = float(str(cm["年化收益率"]).strip("%"))
                sr = float(cm["夏普比率"])
                dd = float(str(cm["最大回撤"]).strip("%"))
                nt = len(m.get("halt_triggers", []))
                print(f"{-th*100:>7.0f}%{label:<12}{ar:>+8.2f}%{sr:>8.2f}"
                      f"{dd:>9.2f}%{nt:>6}", flush=True)


if __name__ == "__main__":
    main()
