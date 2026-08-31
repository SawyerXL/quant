"""
MA200 五档抗噪机制 A/B（2026-08-30, 专家事前设计）。

悖论预警（专家）: 用新参数修参数敏感性, 可能只是把敏感性搬了家。
成功标准 ≠ 收益更高; 成功 = 加入机制后, 原阈值±0.02扰动的离散度收窄。

三机制互斥测试（不叠加）: A确认日数 / B滞回带 / C比值EMA。
内置不对称: 降档即时、升档需确认（避损快、追涨慢, 与择时价值来源一致, 零额外参数）。
同时输出切档次数(滞回天然降换手, 成本命门下的隐藏加分项)。

判读矩阵（事前锁定）:
  敏感性收窄+收益持平      → 采纳最简机制, 联动池第一关重开
  敏感性收窄+收益降>1pp    → 呈现权衡, 用户定夺
  敏感性不收窄             → 问题在五档结构本身 → 转目标波动率控仓(参数8→1)
  仅特定带宽收窄           → 带宽过拟合 → 不采纳, 同样转目标波动率
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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天")

    idx = load_meta("csi800_index")
    ic = idx.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    variants = [
        ("基线(无机制)", {}),
        ("A 确认N=3", {"ma200_confirm_days": 3}),
        ("A 确认N=5", {"ma200_confirm_days": 5}),
        ("B 滞回1%", {"ma200_hysteresis": 0.01}),
        ("B 滞回2%", {"ma200_hysteresis": 0.02}),
        ("B 滞回3%", {"ma200_hysteresis": 0.03}),
        ("C 平滑5日", {"ma200_smooth_days": 5}),
        ("C 平滑10日", {"ma200_smooth_days": 10}),
    ]

    for wname, lo, hi in [("全期2019-2026.8", START, END), ("近段2022-2026.8", "2022-01-01", END)]:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n{'='*74}\n窗口 {wname}\n{'='*74}")
        print(f"{'配置':<16}{'年化':>9}{'夏普':>8}{'回撤':>9}{'切档次数':>9}")
        for name, kw in variants:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **kw})
            nav, m = run_backtest(p, a, rebal, cfg, ic)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            sw = m.get("tier_switch_count", 0)
            print(f"{name:<16}{ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%{sw:>9}")


if __name__ == "__main__":
    main()
