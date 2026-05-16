"""
策略C：在策略A（Formula I）基础上的优化版本。

优化点：
  1. 最小换仓阈值（MIN_SWAP_EDGE）：新股票得分需比将被替换股高出 X% 才真正换
     → 减少无效交易，降低摩擦成本
  2. ROE 质量过滤（ROE_FILTER）：剔除近期 ROE < 0 的亏损公司
     → 减少"动量虚假信号"，提升选股精度
  3. 持仓数量对比（N_HOLDINGS）：分别测试 20 / 25 / 30 只

运行（单次）：
    python scripts/run_backtest_c.py

对比运行：
    python scripts/run_backtest_c.py --compare
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta, load_financial

# ── 复用 A 的核心函数 ───────────────────────────────────────────────────────
from run_backtest_a import (
    load_panels, build_regime_series, _zscore,
    get_monthly_rebalance_dates, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, LIQUIDITY_THRESH,
    MA_PERIOD, REGIME_BEAR_THR, REGIME_BULL_THR,
    PERIOD_STOP, TRAILING_STOP, CASH_YIELD,
)

logger.add("logs/backtest_c.log", rotation="1 day", retention="30 days")

# ── 策略C 专属参数 ─────────────────────────────────────────────────────────
BACKTEST_END  = os.getenv("BACKTEST_END", "")
N_HOLDINGS    = int(os.getenv("N_HOLDINGS", "30"))   # 可设 20/25/30
MIN_SWAP_EDGE = float(os.getenv("MIN_SWAP_EDGE", "0.10"))  # 换仓阈值 10%
USE_ROE       = os.getenv("USE_ROE", "1") == "1"     # 是否启用 ROE 过滤
ROE_MIN       = float(os.getenv("ROE_MIN", "0.0"))    # ROE 最低门槛（<0=亏损剔除）
USE_REGIME    = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ    = os.getenv("REBAL_FREQ", "biweekly")


# ── ROE 数据加载 ────────────────────────────────────────────────────────────
def load_roe_panel(codes: list[str], as_of: str) -> pd.Series:
    """
    加载截面 ROE：取各股最新一期年化 ROE。
    as_of: 截止日期，只取该日前最新披露的季报。
    返回 Series: index=code, value=roe_annualized
    """
    roe_dict = {}
    for code in codes:
        df = load_financial(code)
        if df.empty or "roe_annualized" not in df.columns:
            continue
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
        df = df[df["report_date"] <= pd.Timestamp(as_of)].dropna(subset=["roe_annualized"])
        if df.empty:
            continue
        latest_roe = float(df.sort_values("report_date").iloc[-1]["roe_annualized"])
        roe_dict[code] = latest_roe
    return pd.Series(roe_dict)


# ── 核心：得分计算（与A相同，加ROE过滤）──────────────────────────────────
def compute_score_c(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None = None,
    stock_info: pd.DataFrame | None = None,
    roe_series: pd.Series | None = None,
) -> pd.Series:
    """策略C得分：Formula I + ROE 质量过滤。"""
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    # 流动性过滤
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        ra = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p, p_126 = hist.iloc[-1], hist.iloc[-126]
    high_250 = hist.iloc[-250:].max()
    mom      = p / p_126 - 1
    price_nh = (p / high_250).clip(0.5, 1.2)

    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        vr = ha.iloc[-20:].mean()
        vb = ha.iloc[-250:].mean().replace(0, float("nan"))
        vol_ratio  = (vr / vb).clip(0.5, 3.0)
        vol_recent = vr
    else:
        vol_recent = pd.Series(1.0, index=p.index)
        vol_ratio  = pd.Series(1.0, index=p.index)

    boost      = ((price_nh - 0.9) * 2).clip(0, 1) * ((vol_ratio - 1) * 0.5).clip(0, 0.5)
    base_score = mom * (1 + boost)

    cross_rank = vol_recent.rank(pct=True).reindex(p.index)
    if stock_info is not None and "industry_l1" in stock_info.columns:
        ind_map = stock_info.set_index("code")["industry_l1"]
        sec_rank = pd.Series(0.5, index=p.index)
        for ind in ind_map.unique():
            ic = [c for c in ind_map[ind_map == ind].index
                  if c in p.index and c in vol_recent.index]
            if len(ic) >= 3:
                sec_rank[ic] = vol_recent[ic].rank(pct=True)
        combined = 0.70 * cross_rank + 0.30 * sec_rank.reindex(p.index)
    else:
        combined = cross_rank

    tm    = (0.80 + 0.20 * combined).fillna(0.90)
    score = (base_score * tm).dropna()

    # ── ROE 质量过滤：剔除最新 ROE < ROE_MIN 的亏损/低质量股 ──────────
    if USE_ROE and roe_series is not None and not roe_series.empty:
        valid = roe_series.reindex(score.index).fillna(0) >= ROE_MIN
        score = score[valid]

    return score


# ── 核心：最小换仓阈值 ─────────────────────────────────────────────────────
def apply_swap_threshold(
    score: pd.Series,
    current_holdings: list[str],
    n: int,
    threshold: float,
) -> list[str]:
    """
    懒惰再平衡：只有当替换者得分比被替换者高出 threshold 比例才真正换。
    完全空仓时直接取前 N。
    """
    if not current_holdings or threshold <= 0:
        return score.nlargest(n).index.tolist()

    cur_set   = set(current_holdings)
    top_n     = score.nlargest(n).index.tolist()
    top_set   = set(top_n)

    # 当前持仓但不在 top-N 的（候选剔除，得分从低到高）
    remove_cands = sorted(
        [c for c in current_holdings if c not in top_set],
        key=lambda c: score.get(c, -np.inf)
    )
    # top-N 中当前没有的（候选加入，得分从高到低）
    add_cands = sorted(
        [c for c in top_n if c not in cur_set],
        key=lambda c: score.get(c, np.inf), reverse=True
    )

    result = list(cur_set)

    for rm, add in zip(remove_cands, add_cands):
        rm_score  = score.get(rm,  -np.inf)
        add_score = score.get(add,  np.inf)
        # 只有加入者得分高出 threshold 比例才换
        if rm_score > 0 and add_score / rm_score - 1 >= threshold:
            result.remove(rm)
            result.append(add)
        elif rm_score <= 0 and add_score > 0:
            result.remove(rm)
            result.append(add)
        # 否则保留原股

    # 补齐：若持仓数不足 n，从 top_n 补充
    for c in top_n:
        if len(result) >= n:
            break
        if c not in result:
            result.append(c)

    return result[:n]


# ── 主回测循环 ─────────────────────────────────────────────────────────────
def run_backtest_c(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None = None,
    regime: pd.Series | None = None,
    stock_info: pd.DataFrame | None = None,
) -> pd.Series:
    all_dates   = panel.index
    port_rets   = pd.Series(0.0, index=all_dates)
    cur_hold    = []
    cumul_nav   = 1.0
    entry_hwm   = 1.0
    nav_since   = 1.0
    rebal_set   = set(str(d.date()) if hasattr(d, 'date') else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())
        in_bull  = True

        if regime is not None and date in regime.index:
            in_bull = bool(regime.loc[date])

        if date_str in rebal_set and i >= MIN_BARS:
            if not in_bull:
                cur_hold = []
                nav_since = 1.0
                entry_hwm = cumul_nav
            else:
                # 加载 ROE（截至当前日期）
                roe = None
                if USE_ROE and stock_info is not None:
                    roe = load_roe_panel(panel.columns.tolist(), date_str)

                score = compute_score_c(panel, date, amount_panel, stock_info, roe)
                if len(score) >= N_HOLDINGS:
                    new_hold = apply_swap_threshold(score, cur_hold, N_HOLDINGS, MIN_SWAP_EDGE)
                    # 换仓手续费
                    if cur_hold:
                        old_set = set(cur_hold)
                        new_set = set(new_hold)
                        turnover = (len(old_set - new_set) + len(new_set - old_set)) / (2 * N_HOLDINGS)
                        port_rets.iloc[i] -= turnover * COMMISSION * 2
                    if not cur_hold:
                        entry_hwm = cumul_nav
                    cur_hold  = new_hold
                nav_since = 1.0

        # 止损
        if cur_hold and i > 0:
            period_stop   = nav_since <= (1 + PERIOD_STOP)
            trailing_stop = (cumul_nav / entry_hwm - 1) <= TRAILING_STOP
            if period_stop or trailing_stop:
                cur_hold  = []
                nav_since = 1.0
                entry_hwm = cumul_nav

        # 日收益
        if cur_hold and i > 0:
            prev  = panel.iloc[i - 1][cur_hold].dropna()
            curr  = panel.iloc[i][cur_hold].dropna()
            comm  = prev.index.intersection(curr.index)
            if len(comm) > 0:
                port_rets.iloc[i] += (curr[comm] / prev[comm] - 1).mean()
        elif not cur_hold and CASH_YIELD > 0:
            port_rets.iloc[i] += CASH_YIELD / 252

        nav_since = nav_since * (1 + port_rets.iloc[i])
        cumul_nav = cumul_nav * (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()


# ── 对比报告 ────────────────────────────────────────────────────────────────
def compare(results: dict) -> pd.DataFrame:
    rows = []
    for name, nav in results.items():
        m = calc_metrics(nav)
        rows.append({
            "策略":    name,
            "总收益":  m["总收益率"],
            "年化":    m["年化收益率"],
            "波动率":  m["年化波动率"],
            "夏普":    m["夏普比率"],
            "最大回撤": m["最大回撤"],
            "月度胜率": m["月度胜率"],
        })
    return pd.DataFrame(rows).set_index("策略")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true", help="同时运行A/C对比")
    args = parser.parse_args()

    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        BACKTEST_END = sorted(cal_df["trade_date"].tolist())[-1]

    logger.info("=" * 60)
    logger.info(f"策略C回测  {BACKTEST_START}→{BACKTEST_END}")
    logger.info(f"持仓:{N_HOLDINGS}  换仓阈值:{MIN_SWAP_EDGE:.0%}  ROE过滤:{USE_ROE}  ROE_MIN:{ROE_MIN}")
    logger.info("=" * 60)

    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]

    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())

    logger.info("加载价格+成交额矩阵...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, BACKTEST_END)

    stock_info = load_meta("stock_info_full")
    if stock_info.empty:
        stock_info = None

    regime = None
    if USE_REGIME:
        regime = build_regime_series(BACKTEST_START, BACKTEST_END)
        if regime.empty:
            regime = None

    rebal_dates = get_monthly_rebalance_dates(trade_calendar)

    results = {}

    # ── 运行策略C ──
    logger.info("运行策略C...")
    nav_c = run_backtest_c(panel, rebal_dates, amount_panel, regime, stock_info)
    results["策略C"] = nav_c

    year_end = int(BACKTEST_END[:4])
    logger.info("── 策略C 分年度 ──")
    for year in range(2019, year_end + 1):
        yn = nav_c[nav_c.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:.1%}  最大回撤:{yd:.1%}")

    metrics_c = calc_metrics(nav_c)
    logger.info("── 策略C 总体 ──")
    for k, v in metrics_c.items():
        logger.info(f"  {k}: {v}")

    # 保存净值
    nav_c.to_csv("logs/backtest_c_nav.csv", header=["nav"])

    # ── 若 --compare，同时加载A的结果做对比 ──
    if args.compare:
        nav_a_file = Path("logs/backtest_a_nav_I.csv")
        if nav_a_file.exists():
            nav_a = pd.read_csv(nav_a_file, index_col=0, parse_dates=True)["nav"]
            nav_a.name = "nav"
            results["策略A"] = nav_a

            logger.info("=" * 60)
            logger.info("策略A vs 策略C 对比")
            logger.info("=" * 60)
            cmp = compare(results)
            logger.info(f"\n{cmp.to_string()}")
            print("\n" + "=" * 55)
            print("  策略A vs 策略C 对比")
            print("=" * 55)
            print(cmp.to_string())

            # 逐年对比
            print("\n── 逐年收益对比 ──")
            for year in range(2019, year_end + 1):
                ya = nav_a[nav_a.index.year == year]
                yc = nav_c[nav_c.index.year == year]
                if len(ya) < 2 or len(yc) < 2:
                    continue
                ra = ya.iloc[-1]/ya.iloc[0]-1
                rc = yc.iloc[-1]/yc.iloc[0]-1
                diff = rc - ra
                mark = "↑C优" if diff > 0.01 else ("↓A优" if diff < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  C={rc:+.1%}  差={diff:+.1%} {mark}")
        else:
            print("策略A净值文件不存在，请先跑 run_backtest_a.py")

    ar = float(metrics_c["年化收益率"].strip("%")) / 100
    md = float(metrics_c["最大回撤"].strip("%")) / 100
    sr = float(metrics_c["夏普比率"])
    passed = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if passed else '⚠️  未达标'} (年化≥15% 回撤≥-25% 夏普≥1.0)")


if __name__ == "__main__":
    main()
