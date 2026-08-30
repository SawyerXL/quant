"""
动量池 A/B（评审P1-4: 成交额TOP是拥挤度因子而非动量因子）。

变体:
  A. 基线: 成交额TOP60 + 波动率过滤(现定案)
  B. 动量池60日: 成交额TOP300流动性池内按60日收益排序取60 + 过滤
  C. 动量池120日: 同上, 回看120日
  D. 动量池60日 无过滤(检验: 动量池是否让过滤器变得不必要)
窗口: 全期2019-2026.8 + 近段2022-2026.8 (信号有效再跑2015-2018 OOS)

═══ 事前预测登记（2026-08-30 专家预测, 结果出来前锁定, 防事后解读被结果牵着走）═══
1. A股中期动量弱、短期反转强 → B(60日)五五开; C(120日)略优于B但优势有限
2. D大概率跑输B: 动量排序选出的票波动率天然偏高, 过滤器在动量池上未必失效
3. 设计缺口: 动量应skip最近5-10日(12-1逻辑)规避反转污染。若B/C平庸,
   先补E变体(skip-5d)再下结论, 不直接判动量池死刑
4. 换手率是比收益更关键的输出: 动量排名善变(双周或换1/3), 成本已证实为命门
   (每+0.07pp≈-1pp年化)。结论必须在0.13%和0.30%两档成本下都成立才算数

═══ 判读矩阵（事前锁定）═══
  B/C双窗口赢A且成本压测后仍赢 → 拥挤度假说成立, 动量池是升级方向 → 补OOS+skip-d精调
  B/C赢A但换手率抹平优势       → 信号对但执行不经济 → 试月频调仓+动量池组合
  B/C≈A                        → 池子构造不是敏感维度 → 关闭此项, 资源转pool_size OOS
  B/C输A                        → A股动量弱经典重现 → 先补skip-5d排除反转污染, 再关闭
  D≈B                           → 过滤器只在拥挤池上有意义 → 文档标注过滤器适用边界
  D明显输B                      → 过滤器是通用风控 → 过滤器地位升级为独立模块
跨窗口排序反转 = 信号不稳 = 不采纳（与熔断定案同一标准）
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
WINDOWS = [("全期2019-2026.8", "2019-01-01", "2026-08-28"),
           ("近段2022-2026.8", "2022-01-01", "2026-08-28")]


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
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    variants = [
        ("A 成交额TOP60+过滤(基线)", DEFAULT_CONFIG),
        ("B 动量60日+过滤", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_style": "momentum", "mom_window": 60})),
        ("C 动量120日+过滤", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_style": "momentum", "mom_window": 120})),
        ("D 动量60日无过滤", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_style": "momentum", "mom_window": 60, "max_vol20": 999})),
    ]

    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n{'='*70}\n窗口 {wname}\n{'='*70}")
        print(f"{'配置':<24}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
        for name, cfg in variants:
            nav, _ = run_backtest(p, a, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<24}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%")


if __name__ == "__main__":
    main()
