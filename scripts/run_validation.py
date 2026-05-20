"""
策略 A-4 全方位验证框架
包含：Walk Forward / CPCV / Robustness & Stress / Block Bootstrap

运行：
    python scripts/run_validation.py
    python scripts/run_validation.py --fast   # 快速版（减少迭代次数）
"""
import os, sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from itertools import combinations
from loguru import logger

from run_backtest_a4 import run_backtest_a4, select_dynamic_grace
from run_backtest_a2 import (
    compute_score_a2, compute_weights, get_position_ratio,
    _make_rebal_dates, MAX_IND_SLOT, SECTOR_BOOST,
)
from run_backtest_a import (
    load_panels, calc_metrics, COMMISSION, MIN_BARS,
    CASH_YIELD, PERIOD_STOP, TRAILING_STOP, MA_PERIOD,
)
from data.storage import load_meta

logger.add("logs/validation.log", rotation="1 day")

# ── 压力测试区间 ────────────────────────────────────────────────────────
STRESS_PERIODS = {
    "2016熔断恢复": ("2016-01-01", "2016-12-31"),
    "2017漂亮50牛": ("2017-01-01", "2017-12-31"),
    "2018大熊市":   ("2018-01-01", "2018-12-31"),
    "2020新冠暴跌": ("2020-01-01", "2020-06-30"),
    "2021抱团崩盘": ("2021-02-01", "2021-09-30"),
    "2022全面熊":   ("2022-01-01", "2022-12-31"),
    "2024政策刺激": ("2024-07-01", "2024-12-31"),
}


# ── 工具函数 ─────────────────────────────────────────────────────────────

def _run_period(panel, amt, idx_close, stock_info, start, end,
                rebal_freq="biweekly", eval_start=None):
    """
    在指定区间运行 A-4 回测，返回 NAV Series。
    eval_start: 只统计该日期之后的 NAV（WFA 用，含 IS 期预热但只评估 OOS）
    """
    cal = load_meta("trade_calendar")
    calendar = [d for d in sorted(cal["trade_date"].tolist()) if start <= d <= end]
    if len(calendar) < 100:
        return pd.Series(dtype=float)
    rebal_dates = _make_rebal_dates(calendar, rebal_freq)
    sub_panel   = panel[start:end]
    sub_amt     = amt[start:end] if amt is not None else None
    sub_idx     = idx_close[start:end] if idx_close is not None else None
    nav = run_backtest_a4(sub_panel, rebal_dates, sub_amt, sub_idx, stock_info)
    if eval_start and not nav.empty:
        nav = nav[nav.index >= pd.Timestamp(eval_start)]
        if not nav.empty:
            nav = nav / nav.iloc[0]   # 重新归一到1.0
    return nav


def _monthly_returns(nav: pd.Series) -> pd.Series:
    return nav.resample("ME").last().pct_change().dropna()


def _sharpe(rets: pd.Series) -> float:
    if rets.std() < 1e-8:
        return 0.0
    return rets.mean() / rets.std() * np.sqrt(12)   # 月度 → 年化


def _max_dd(nav: pd.Series) -> float:
    return float(((nav - nav.cummax()) / nav.cummax()).min())


# ── 1. Walk Forward Analysis ─────────────────────────────────────────────

def walk_forward(panel, amt, idx_close, stock_info, fast=False):
    logger.info("=" * 60)
    logger.info("Walk Forward Analysis")
    logger.info("  IS=3年  OOS=1年  滑动步长=6个月")
    logger.info("=" * 60)

    starts = pd.date_range("2016-01-01", "2022-07-01", freq="6MS")
    if fast:
        starts = starts[::2]   # 快速模式：隔一个取

    rows = []
    for s in starts:
        is_start = str(s.date())
        is_end   = str((s + pd.DateOffset(years=3)).date())
        oos_end  = str((s + pd.DateOffset(years=4)).date())

        if oos_end > "2025-12-31":
            break

        # IS：从 is_start 跑到 is_end
        nav_is = _run_period(panel, amt, idx_close, stock_info, is_start, is_end)
        # OOS：从 is_start 起加载数据（含 IS 期预热），但只统计 is_end 之后的表现
        nav_oos = _run_period(panel, amt, idx_close, stock_info,
                              is_start, oos_end, eval_start=is_end)

        if nav_is.empty or nav_oos.empty:
            continue

        m_is  = calc_metrics(nav_is)
        m_oos = calc_metrics(nav_oos)

        ar_is  = float(m_is["年化收益率"].strip("%")) / 100
        ar_oos = float(m_oos["年化收益率"].strip("%")) / 100
        sh_is  = float(m_is["夏普比率"])
        sh_oos = float(m_oos["夏普比率"])

        rows.append({
            "IS区间": f"{is_start[:7]}~{is_end[:7]}",
            "OOS区间": f"{is_end[:7]}~{oos_end[:7]}",
            "IS年化":  f"{ar_is:.1%}",
            "OOS年化": f"{ar_oos:.1%}",
            "IS夏普":  f"{sh_is:.2f}",
            "OOS夏普": f"{sh_oos:.2f}",
            "退化率":  f"{(sh_is - sh_oos) / sh_is:.1%}" if sh_is > 0 else "N/A",
        })
        logger.info(f"  {is_end[:7]}OOS: 年化{ar_oos:.1%} 夏普{sh_oos:.2f}")

    df = pd.DataFrame(rows)
    if not df.empty:
        logger.info("\n" + df.to_string(index=False))
        oos_sharpes = [float(r["OOS夏普"]) for r in rows]
        pct_positive = sum(1 for s in oos_sharpes if s > 0) / len(oos_sharpes)
        avg_oos_sh   = np.mean(oos_sharpes)
        logger.info(f"\n  OOS夏普均值: {avg_oos_sh:.2f}  OOS正夏普比例: {pct_positive:.0%}")

    return df


# ── 2. CPCV（组合清洗交叉验证）────────────────────────────────────────────

def cpcv(nav_full: pd.Series, n_splits=8, k_test=2, embargo_months=1):
    """
    基于完整 NAV 序列做 CPCV。
    - 将月度收益序列切为 n_splits 段
    - 每次选 k_test 段作为测试集（共 C(n,k) 个组合）
    - 相邻段间加 embargo_months 个月的边界
    """
    logger.info("=" * 60)
    logger.info(f"CPCV  n_splits={n_splits}  k_test={k_test}  embargo={embargo_months}月")
    logger.info("=" * 60)

    monthly = _monthly_returns(nav_full).dropna()
    months  = list(monthly.index)
    n = len(months)
    groups  = np.array_split(months, n_splits)

    combo_sharpes, combo_returns = [], []

    for test_idx in combinations(range(n_splits), k_test):
        test_months_raw = [m for i in test_idx for m in groups[i]]

        # 加 embargo：去掉每段测试集首尾 embargo 个月
        test_months = []
        for i in test_idx:
            grp = list(groups[i])
            start_m = grp[embargo_months] if len(grp) > embargo_months else grp[0]
            end_m   = grp[-(embargo_months+1)] if len(grp) > embargo_months else grp[-1]
            test_months.extend([m for m in grp if start_m <= m <= end_m])

        if not test_months:
            continue

        rets = monthly[monthly.index.isin(test_months)]
        if len(rets) < 3:
            continue

        sh  = _sharpe(rets)
        ar  = (1 + rets.mean()) ** 12 - 1
        combo_sharpes.append(sh)
        combo_returns.append(ar)

    n_combos = len(combo_sharpes)
    if n_combos == 0:
        logger.warning("CPCV: 无有效组合")
        return {}

    pct_positive = sum(1 for s in combo_sharpes if s > 0) / n_combos
    logger.info(f"  组合总数:    {n_combos} (C({n_splits},{k_test})={len(list(combinations(range(n_splits),k_test)))})")
    logger.info(f"  夏普均值:    {np.mean(combo_sharpes):.2f}")
    logger.info(f"  夏普中位数:  {np.median(combo_sharpes):.2f}")
    logger.info(f"  夏普标准差:  {np.std(combo_sharpes):.2f}")
    logger.info(f"  夏普>0 比例: {pct_positive:.0%}  (低于80%说明策略不稳健)")
    logger.info(f"  年化均值:    {np.mean(combo_returns):.1%}")
    logger.info(f"  年化中位数:  {np.median(combo_returns):.1%}")

    # 5th/95th percentile
    p5  = np.percentile(combo_sharpes, 5)
    p95 = np.percentile(combo_sharpes, 95)
    logger.info(f"  夏普 5%/95%: {p5:.2f} / {p95:.2f}")

    return {
        "n_combos": n_combos,
        "sharpe_mean": np.mean(combo_sharpes),
        "sharpe_median": np.median(combo_sharpes),
        "pct_positive": pct_positive,
        "return_mean": np.mean(combo_returns),
        "sharpe_p5": p5,
        "sharpe_p95": p95,
    }


# ── 3. Robustness & Stress Testing ───────────────────────────────────────

def robustness_stress(panel, amt, idx_close, stock_info):
    logger.info("=" * 60)
    logger.info("Robustness & Stress Testing")
    logger.info("=" * 60)

    rows = []
    for name, (s, e) in STRESS_PERIODS.items():
        nav = _run_period(panel, amt, idx_close, stock_info, s, e)
        if nav.empty or len(nav) < 20:
            continue
        m = calc_metrics(nav)
        ar = float(m["年化收益率"].strip("%")) / 100
        sh = float(m["夏普比率"])
        dd = float(m["最大回撤"].strip("%")) / 100
        rows.append({"区间": name, "起止": f"{s[:7]}~{e[:7]}",
                     "年化": f"{ar:.1%}", "夏普": f"{sh:.2f}", "最大回撤": f"{dd:.1%}"})
        logger.info(f"  {name:12s}: 年化{ar:.1%}  夏普{sh:.2f}  回撤{dd:.1%}")

    # 参数敏感性（MA10出清天数）
    logger.info("\n  参数敏感性（MA10出清天数 vs 主回测区间 2019-2024）")
    cal = load_meta("trade_calendar")
    calendar = [d for d in sorted(cal["trade_date"].tolist())
                if "2019-01-01" <= d <= "2024-12-31"]
    rebal_dates = _make_rebal_dates(calendar, "biweekly")
    sub_panel = panel["2019-01-01":"2024-12-31"]
    sub_amt   = amt["2019-01-01":"2024-12-31"] if amt is not None else None
    sub_idx   = idx_close["2019-01-01":"2024-12-31"] if idx_close is not None else None

    from run_backtest_a4 import MA10_EXIT_DAYS as DEFAULT_DAYS
    import run_backtest_a4 as a4_mod

    sensitivity_rows = []
    for days in [2, 3, 4, 5, 7]:
        a4_mod.MA10_EXIT_DAYS = days
        nav = run_backtest_a4(sub_panel, rebal_dates, sub_amt, sub_idx, stock_info)
        if nav.empty:
            continue
        m  = calc_metrics(nav)
        ar = float(m["年化收益率"].strip("%")) / 100
        sh = float(m["夏普比率"])
        dd = float(m["最大回撤"].strip("%")) / 100
        sensitivity_rows.append(f"  MA10天数={days}: 年化{ar:.1%} 夏普{sh:.2f} 回撤{dd:.1%}")
        logger.info(sensitivity_rows[-1])

    a4_mod.MA10_EXIT_DAYS = DEFAULT_DAYS   # 恢复默认

    df = pd.DataFrame(rows)
    if not df.empty:
        logger.info("\n" + df.to_string(index=False))
    return df


# ── 4. Block Bootstrap（合成数据） ────────────────────────────────────────

def block_bootstrap(nav_full: pd.Series, n_sim=1000, block_size=21, fast=False):
    """
    块自举法：从实际月度收益序列中有放回地抽取 block_size 天的块，
    拼接成等长的模拟路径，统计策略在随机顺序下的表现分布。
    """
    if fast:
        n_sim = 200

    logger.info("=" * 60)
    logger.info(f"Block Bootstrap  n_sim={n_sim}  block_size={block_size}天")
    logger.info("=" * 60)

    # 使用日度收益
    daily_rets = nav_full.pct_change().dropna()
    n = len(daily_rets)
    n_blocks = n // block_size + 1
    block_starts = list(range(0, n - block_size + 1))

    sim_sharpes, sim_maxdds, sim_returns = [], [], []

    rng = np.random.default_rng(42)
    for _ in range(n_sim):
        # 随机抽取 n_blocks 个块拼接
        idx = rng.choice(block_starts, size=n_blocks, replace=True)
        sim_rets = np.concatenate([daily_rets.values[i:i+block_size] for i in idx])[:n]
        sim_nav  = (1 + pd.Series(sim_rets)).cumprod()

        total  = sim_nav.iloc[-1] - 1
        ann    = (1 + total) ** (252 / n) - 1
        vol    = pd.Series(sim_rets).std() * np.sqrt(252)
        sharpe = ann / vol if vol > 0 else 0
        mdd    = float(((sim_nav - sim_nav.cummax()) / sim_nav.cummax()).min())

        sim_sharpes.append(sharpe)
        sim_maxdds.append(mdd)
        sim_returns.append(ann)

    real_sharpe = float(calc_metrics(nav_full)["夏普比率"])
    pct_above   = sum(1 for s in sim_sharpes if s >= real_sharpe) / n_sim
    p_value     = pct_above   # 真实夏普超过模拟的比例 → 越低越好

    logger.info(f"  真实策略夏普:   {real_sharpe:.2f}")
    logger.info(f"  模拟夏普均值:   {np.mean(sim_sharpes):.2f}")
    logger.info(f"  模拟夏普中位数: {np.median(sim_sharpes):.2f}")
    logger.info(f"  模拟夏普标准差: {np.std(sim_sharpes):.2f}")
    logger.info(f"  p-value（模拟 ≥ 真实）: {p_value:.1%}  (< 5% 说明策略有显著alpha)")
    logger.info(f"  模拟最大回撤均值: {np.mean(sim_maxdds):.1%}")
    logger.info(f"  模拟年化收益均值: {np.mean(sim_returns):.1%}")

    # 超越随机的概率
    pct_bs_positive = sum(1 for s in sim_sharpes if s > 0) / n_sim
    logger.info(f"  随机路径夏普>0比例: {pct_bs_positive:.0%}")

    return {
        "real_sharpe": real_sharpe,
        "sim_sharpe_mean": np.mean(sim_sharpes),
        "p_value": p_value,
        "significant": p_value < 0.05,
    }


# ── 主函数 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="快速模式（减少迭代次数）")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"策略 A-4 全方位验证  {'（快速模式）' if args.fast else ''}")
    logger.info("=" * 60)

    # 加载数据（共用，避免重复IO）
    logger.info("加载数据...")
    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())
    panel, amt = load_panels(codes, "2015-07-01", "2025-12-31")
    logger.info(f"价格矩阵: {panel.shape[0]}天 × {panel.shape[1]}只")

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        idx_close = None
    else:
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        idx_close = idx_df.set_index("date")["close"].sort_index()

    # 先跑完整回测拿到 NAV
    logger.info("\n加载完整回测 NAV（2016-2025）...")
    nav_path = Path("logs/backtest_a4_nav.csv")
    if nav_path.exists():
        nav_full = pd.read_csv(nav_path, index_col=0, parse_dates=True)["nav"]
        nav_full = nav_full["2016-01-01":"2025-12-31"]
        logger.info(f"  从缓存加载 NAV: {len(nav_full)} 天")
    else:
        logger.info("  重新运行全量回测...")
        from run_backtest_a import BACKTEST_START
        cal = load_meta("trade_calendar")
        calendar = [d for d in sorted(cal["trade_date"].tolist())
                    if "2016-01-01" <= d <= "2025-12-31"]
        rebal = _make_rebal_dates(calendar, "biweekly")
        nav_full = run_backtest_a4(
            panel["2016-01-01":"2025-12-31"],
            rebal, amt["2016-01-01":"2025-12-31"], idx_close, stock_info
        )
        nav_full.to_csv(nav_path, header=["nav"])
        logger.info(f"  NAV 已保存: {len(nav_full)} 天")

    m = calc_metrics(nav_full)
    logger.info(f"\n基准指标（2016-2025）:")
    logger.info(f"  年化: {m['年化收益率']}  夏普: {m['夏普比率']}  回撤: {m['最大回撤']}")

    # ── 1. Walk Forward ─────────────────────────────────
    wf_df = walk_forward(panel, amt, idx_close, stock_info, fast=args.fast)

    # ── 2. CPCV ─────────────────────────────────────────
    cpcv_results = cpcv(nav_full, n_splits=8, k_test=2, embargo_months=1)

    # ── 3. Robustness & Stress ───────────────────────────
    stress_df = robustness_stress(panel, amt, idx_close, stock_info)

    # ── 4. Block Bootstrap ───────────────────────────────
    bs_results = block_bootstrap(nav_full, n_sim=1000 if not args.fast else 200,
                                  block_size=21, fast=args.fast)

    # ── 综合结论 ─────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("综合验证结论")
    logger.info("=" * 60)

    checks = []

    if not wf_df.empty:
        oos_sharpes = [float(r["OOS夏普"]) for _, r in wf_df.iterrows()]
        wf_pct = sum(1 for s in oos_sharpes if s > 0) / len(oos_sharpes)
        status = "✅" if wf_pct >= 0.7 else "⚠️"
        checks.append(f"{status} Walk Forward OOS正夏普比例: {wf_pct:.0%} (≥70%=通过)")

    if cpcv_results:
        status = "✅" if cpcv_results["pct_positive"] >= 0.8 else "⚠️"
        checks.append(f"{status} CPCV 正夏普比例: {cpcv_results['pct_positive']:.0%} (≥80%=通过)")
        status = "✅" if cpcv_results["sharpe_mean"] > 0.5 else "⚠️"
        checks.append(f"{status} CPCV 夏普均值: {cpcv_results['sharpe_mean']:.2f} (≥0.5=通过)")

    if bs_results:
        status = "✅" if bs_results["p_value"] < 0.05 else "⚠️"
        checks.append(f"{status} Bootstrap p值: {bs_results['p_value']:.1%} (<5%=显著alpha)")

    for c in checks:
        logger.info(f"  {c}")

    all_pass = all(c.startswith("  ✅") for c in checks)
    logger.info(f"\n{'🏆 策略通过全部验证，可信度高！' if all_pass else '⚠️  部分指标需要关注，见上方详情'}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
