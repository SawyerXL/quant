"""
熔断触发流程回测 (2026-08-30, 用户问"能用回测选吗")。

三方案: A全清仓 / B暂停开仓(持仓按MA10自然退出) / C触发日降至30%底仓。
恢复规则(人工确认的回测代理): 触发后≥10个交易日 且 从触发后最低点反弹5%。
限制如实声明: -25%回撤10年仅触发2-3次 → 案例路径分析, 非大样本A/B。

窗口: 全期2019-2026.8 + OOS 2015-2018(日历用上证日期, trade_calendar 2019前缺失)
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


def run_window(lo, hi, cal):
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
        panel, ap, idx_c, rebal = run_window(lo, hi, cal)
        print(f"\n{'='*74}\n窗口 {wname}\n{'='*74}")
        print(f"{'方案':<16}{'年化':>9}{'夏普':>8}{'最大回撤':>10}{'触发':>6}  触发详情")
        for mode, label in [("none", "无熔断(对照)"), ("A", "A全清仓"),
                            ("B", "B暂停开仓"), ("C", "C降30%底仓")]:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "halt_mode": mode})
            nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            tr = m.get("halt_triggers", [])
            print(f"{label:<16}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%{len(tr):>6}   "
                  f"{[f'{t[0]}(nav{t[1]:.3f})' for t in tr[:3]]}")


if __name__ == "__main__":
    main()
