"""
rank buffer + MA10 重入冷却 A/B (2026-08-31)。

事前锁定(专家):
  - rank buffer: 进60/出{78,90,102}, 预测换手降15~30%收益持平或微升;
    风险=出场线放宽后持仓新鲜度下降伤动量弹性
  - 重入冷却 N∈{5,10,20}: 预测效果偏小方向为正, 高成本档下更明显;
    风险=冷却期错过V反(与MA10判弱的票V反概率低对冲)
  两项独立测不叠加。采纳标准: 双窗口+双成本档(0.13%/0.30%)收益不劣
  +换手降幅>15%, 网格须平台。
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


def main():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    idx = load_meta("csi800_index")
    ic = idx.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    variants = [
        ("基线", {}),
        ("rank 1.3(出78)", {"rank_buffer_mult": 1.3}),
        ("rank 1.5(出90)", {"rank_buffer_mult": 1.5}),
        ("rank 1.7(出102)", {"rank_buffer_mult": 1.7}),
        ("冷却N=5", {"ma10_reentry_cool": 5}),
        ("冷却N=10", {"ma10_reentry_cool": 10}),
        ("冷却N=20", {"ma10_reentry_cool": 20}),
    ]

    for wname, lo, hi in [("全期2019-2026.8", START, END), ("近段2022-2026.8", "2022-01-01", END)]:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n=== {wname} ===", flush=True)
        print(f"{'配置':<16}{'年化':>9}{'夏普':>8}{'回撤':>9}{'年成本损耗':>10}", flush=True)
        for name, kw in variants:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **kw})
            nav, m = run_backtest(p, a, rebal, cfg, ic)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<16}{ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%"
                  f"{m.get('annual_cost_drag', 0) * 100:>9.2f}%", flush=True)


if __name__ == "__main__":
    main()
