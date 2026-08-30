"""
P1 三连跑 (2026-08-30, 专家排期②③④):
  ② MA200敏感性: 阈值±0.02平移 / 熊市档0.30→0.20
  ③ 基准超额三段拆解: 2019-21牛 / 2022-24.6熊震荡 / 2024.7-26.8政策牛+BEAR
  ④ pool_size OOS: 2015-2018 跑 30/40/60/80(+过滤)
一次加载两套面板, 输出全部结果。
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


def load_panel(lo, hi):
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
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(amounts).sort_index()


def idx_close():
    idx = load_meta("csi800_index")
    ic = idx.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    return ic


def cal():
    sh = load_daily("000001", "2014-06-01", "2026-08-28")
    return sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))


def run_line(panel, ap, rebal, cfg, ic, label):
    nav, _ = run_backtest(panel, ap, rebal, cfg, ic)
    cm = calc_metrics(nav)
    ar = float(str(cm["年化收益率"]).strip("%"))
    sr = float(cm["夏普比率"])
    dd = float(str(cm["最大回撤"]).strip("%"))
    print(f"{label:<26}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%", flush=True)


def main():
    ic = idx_close()
    calx = cal()

    # ── ② MA200 敏感性 (全期) ──
    print("=" * 62)
    print("② MA200 敏感性 (全期 2019-2026.8, TOP60+过滤)")
    print("=" * 62)
    panel, ap = load_panel("2019-01-01", "2026-08-28")
    rebal = [d for d in make_rebal_dates(calx, "biweekly") if "2019-01-01" <= d <= "2026-08-28"]
    print(f"{'变体':<26}{'年化':>9}{'夏普':>8}{'回撤':>10}")
    run_line(panel, ap, rebal, DEFAULT_CONFIG, ic, "基线(现网五档)")
    run_line(panel, ap, rebal,
             BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "ma200_thresh_shift": 0.02}), ic, "阈值+0.02(更晚降档)")
    run_line(panel, ap, rebal,
             BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "ma200_thresh_shift": -0.02}), ic, "阈值-0.02(更早降档)")
    run_line(panel, ap, rebal,
             BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "ma200_bear_pos": 0.20}), ic, "熊市档0.30→0.20")
    run_line(panel, ap, rebal,
             BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "ma200_bear_pos": 0.40}), ic, "熊市档0.30→0.40")

    # ── ③ 三段拆解 ──
    print()
    print("=" * 62)
    print("③ 基准超额三段拆解 (策略 vs 中证800)")
    print("=" * 62)
    segs = [("2019-2021 牛", "2019-01-01", "2021-12-31"),
            ("2022-2024.6 熊震荡", "2022-01-01", "2024-06-30"),
            ("2024.7-2026.8 政策牛+BEAR", "2024-07-01", "2026-08-28")]
    print(f"{'段':<24}{'策略年化':>10}{'基准年化':>10}{'策略回撤':>10}{'基准回撤':>10}{'超额':>8}")
    for name, lo, hi in segs:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rb = [d for d in rebal if lo <= d <= hi]
        nav, _ = run_backtest(p, a, rb, DEFAULT_CONFIG, ic)
        cm = calc_metrics(nav)
        sar = float(str(cm["年化收益率"]).strip("%"))
        sdd = float(str(cm["最大回撤"]).strip("%"))
        b = ic[(ic.index >= lo) & (ic.index <= hi)]
        bh = b / b.iloc[0]
        days = (b.index[-1] - b.index[0]).days
        bar = (bh.iloc[-1] ** (365 / days) - 1) * 100
        bdd = (bh / bh.cummax() - 1).min() * 100
        print(f"{name:<24}{sar:>+9.2f}%{bar:>+9.2f}%{sdd:>9.2f}%{bdd:>9.2f}%{sar-bar:>+7.2f}pp")

    # ── ④ pool_size OOS (2015-2018) ──
    print()
    print("=" * 62)
    print("④ pool_size OOS (2015-2018, +过滤)")
    print("=" * 62)
    p2, a2 = load_panel("2015-01-01", "2018-12-31")
    rb2 = [d for d in make_rebal_dates(calx, "biweekly") if "2015-01-01" <= d <= "2018-12-31"]
    print(f"{'变体':<26}{'年化':>9}{'夏普':>8}{'回撤':>10}")
    for ps in (30, 40, 60, 80):
        run_line(p2, a2, rb2,
                 BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_size": ps}), ic, f"TOP{ps}+过滤")


if __name__ == "__main__":
    main()
