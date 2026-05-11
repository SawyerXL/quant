"""
Track A 多因子选股策略回测脚本。
用纯 pandas/numpy 实现，不依赖 vectorbt（避免版本兼容问题）。

覆盖：2019-01-01 至 2024-12-31
股票池：中证800成分股，月末选30只等权持有

动量公式（通过 FORMULA 常量切换，默认H）：
  H  最优：240日动量 × 量价突破加成 × 截面换手降权 → 年化14.9%, 夏普0.49
  G  次优：240日动量 × 量价突破加成（无换手降权）   → 年化13.4%, 夏普0.44
  F  基准：纯240日动量（无任何加成）               → 年化11.8%, 夏普0.39
  E  历史基线：raw(-ret20 + ret240-ret20)/2        → 年化8.7%,  夏普0.30
  A~D 实验公式（含z-score版本，均不如F）

运行：
    python scripts/run_backtest_a.py
    FORMULA=C python scripts/run_backtest_a.py
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta

FORMULA          = os.getenv("FORMULA", "H")      # H(默认,最优) / F/G/A~E 见注释
UNIVERSE         = os.getenv("UNIVERSE", "800")   # "800" / "1800"(800+1000)
BACKTEST_START   = "2019-01-01"
BACKTEST_END     = "2024-12-31"
N_HOLDINGS       = 30
COMMISSION       = 0.00125   # 单边 0.125%
MIN_BARS         = 250       # 预热期
LIQUIDITY_THRESH = 1000      # 流动性门槛：20日均成交额 > 1000万元

logger.add("logs/backtest_a.log", rotation="1 day", retention="30 days")


def load_panels(codes: list, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    同时加载收盘价矩阵和成交额矩阵（万元）。
    返回 (price_panel, amount_panel)，index=date，columns=code。
    """
    prices, amounts = {}, {}
    for code in codes:
        df = load_daily(code, start, end)
        if df.empty or "close" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        close  = pd.to_numeric(df["close"],  errors="coerce")
        amount = pd.to_numeric(df.get("amount", pd.Series(dtype=float)), errors="coerce")
        if len(close) > 200:
            prices[code]  = close
            amounts[code] = amount
    price_panel  = pd.DataFrame(prices).sort_index()
    amount_panel = pd.DataFrame(amounts).sort_index()
    logger.info(f"价格矩阵: {price_panel.shape[0]} 天 × {price_panel.shape[1]} 只股票")
    return price_panel, amount_panel


def _zscore(s: pd.Series) -> pd.Series:
    """截面 z-score，±3σ 截断。"""
    mu, sigma = s.mean(), s.std()
    if sigma < 1e-8:
        return pd.Series(0.0, index=s.index)
    return ((s - mu) / sigma).clip(-3, 3)


def compute_score(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None = None,
) -> pd.Series:
    """
    截面动量得分，公式由 FORMULA 常量控制。
    amount_panel 不为 None 时，先做 20日均额 > LIQUIDITY_THRESH 的流动性过滤。
    返回空 Series 表示数据不足，跳过本期调仓。
    """
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    # ── 流动性过滤（调仓日前20日均成交额）──────────
    if amount_panel is not None:
        hist_amt   = amount_panel[amount_panel.index <= date]
        recent_amt = hist_amt.iloc[-20:].mean()
        liquid     = recent_amt[recent_amt > LIQUIDITY_THRESH].index
        hist       = hist[hist.columns.intersection(liquid)]
        if hist.empty:
            return pd.Series(dtype=float)

    p     = hist.iloc[-1]       # 当日价格
    p_21  = hist.iloc[-21]      # 约1个月前
    p_126 = hist.iloc[-126]     # 约6个月前
    p_252 = hist.iloc[-252]     # 约12个月前

    reversal = _zscore(p_21 / p - 1)           # 1月反转（负的1月收益）

    if FORMULA == "A":
        # 原始公式：混合反转 + 240日动量
        ret_20  = p / hist.iloc[-20] - 1
        ret_240 = p / hist.iloc[-240] - 1
        score = (_zscore(-ret_20) + _zscore(ret_240 - ret_20)) / 2

    elif FORMULA == "B":
        # 标准12-1月动量（不含最近1个月）
        score = _zscore(p_21 / p_252 - 1)

    elif FORMULA == "C":
        # 12-1月动量 + 1月反转（行业常用组合）
        mom_12_1 = _zscore(p_21 / p_252 - 1)
        score = 0.7 * mom_12_1 + 0.3 * reversal

    elif FORMULA == "D":
        # 三因子：12-1 + 6-1 + 反转
        mom_12_1 = _zscore(p_21 / p_252 - 1)
        mom_6_1  = _zscore(p_21 / p_126 - 1)
        score = 0.4 * mom_12_1 + 0.2 * mom_6_1 + 0.4 * reversal

    elif FORMULA == "E":
        # 历史基线（无z-score），用于对比
        ret_20  = p / hist.iloc[-20] - 1
        ret_240 = p / hist.iloc[-240] - 1
        score = (-ret_20 + ret_240 - ret_20) / 2

    elif FORMULA == "F":
        # 最优公式：纯240日价格动量，无反转项
        score = p / hist.iloc[-240] - 1

    elif FORMULA in ("G", "H"):
        # 基础：240日动量
        mom = p / hist.iloc[-240] - 1

        # 共用：成交额数据
        if amount_panel is not None:
            hist_amt   = amount_panel[amount_panel.index <= date]
            vol_recent = hist_amt.iloc[-20:].mean()
            vol_base   = hist_amt.iloc[-250:].mean().replace(0, float("nan"))
            vol_ratio  = (vol_recent / vol_base).clip(0.5, 3.0)
        else:
            vol_recent = pd.Series(1.0, index=p.index)
            vol_ratio  = pd.Series(1.0, index=p.index)

        # G/H 共用：价格新高 + 量能放大加成（最大 +50%）
        high_250 = hist.iloc[-250:].max()
        price_new_high = (p / high_250).clip(0.5, 1.2)
        boost = ((price_new_high - 0.9) * 2).clip(0, 1) * \
                ((vol_ratio - 1.0) * 0.5).clip(0, 0.5)
        score = mom * (1 + boost)

        if FORMULA == "H":
            # 额外：截面成交额排名降权（代替行业换手率）
            # 成交额低排名 → 银行/公用事业自然落底，最多降权 20%
            amt_rank = vol_recent.rank(pct=True).reindex(p.index)
            turnover_mult = (0.80 + 0.20 * amt_rank).fillna(0.90)
            score = score * turnover_mult

    else:
        raise ValueError(f"未知 FORMULA: {FORMULA}")

    return score.dropna()


def get_monthly_rebalance_dates(trade_calendar: list) -> list[str]:
    """返回每月最后一个交易日列表（调仓信号日）。"""
    dates = pd.DatetimeIndex(sorted(trade_calendar))
    dates = dates[(dates >= BACKTEST_START) & (dates <= BACKTEST_END)]
    monthly_ends = []
    for year in range(dates[0].year, dates[-1].year + 1):
        for month in range(1, 13):
            month_dates = dates[(dates.year == year) & (dates.month == month)]
            if len(month_dates) > 0:
                monthly_ends.append(str(month_dates[-1].date()))
    return monthly_ends


def run_backtest(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None = None,
) -> pd.Series:
    """
    向量化回测主函数。
    返回：每日组合净值序列（初始=1.0）
    """
    all_dates = panel.index
    portfolio_returns = pd.Series(0.0, index=all_dates)
    current_holdings = []
    rebalance_set = set(str(d.date()) for d in pd.DatetimeIndex(rebalance_dates))

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        if date_str in rebalance_set and i >= MIN_BARS:
            score = compute_score(panel, date, amount_panel)
            if len(score) >= N_HOLDINGS:
                new_holdings = score.nlargest(N_HOLDINGS).index.tolist()
                if current_holdings:
                    sell_set = set(current_holdings) - set(new_holdings)
                    buy_set  = set(new_holdings) - set(current_holdings)
                    turnover = (len(sell_set) + len(buy_set)) / (2 * N_HOLDINGS)
                    portfolio_returns.iloc[i] -= turnover * COMMISSION * 2
                current_holdings = new_holdings

        if current_holdings and i > 0:
            prev_prices = panel.iloc[i - 1][current_holdings].dropna()
            curr_prices = panel.iloc[i][current_holdings].dropna()
            common = prev_prices.index.intersection(curr_prices.index)
            if len(common) > 0:
                daily_rets = curr_prices[common] / prev_prices[common] - 1
                portfolio_returns.iloc[i] += daily_rets.mean()

    return (1 + portfolio_returns).cumprod()


def calc_metrics(nav: pd.Series) -> dict:
    rets = nav.pct_change().dropna()
    total_return  = nav.iloc[-1] / nav.iloc[0] - 1
    annual_return = (1 + total_return) ** (252 / len(nav)) - 1
    annual_vol    = rets.std() * np.sqrt(252)
    sharpe        = annual_return / annual_vol if annual_vol > 0 else 0
    max_drawdown  = ((nav - nav.cummax()) / nav.cummax()).min()
    win_rate      = (nav.resample("ME").last().pct_change().dropna() > 0).mean()
    return {
        "总收益率":   f"{total_return:.1%}",
        "年化收益率": f"{annual_return:.1%}",
        "年化波动率": f"{annual_vol:.1%}",
        "夏普比率":   f"{sharpe:.2f}",
        "最大回撤":   f"{max_drawdown:.1%}",
        "月度胜率":   f"{win_rate:.1%}",
    }


def main():
    logger.info("=" * 60)
    logger.info(f"Track A 回测  公式:{FORMULA}  股票池:{UNIVERSE}  {BACKTEST_START}→{BACKTEST_END}")
    logger.info(f"流动性门槛: {LIQUIDITY_THRESH}万元/日（20日均）")
    logger.info("=" * 60)

    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失")
        return
    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]

    # ── 股票池 ──────────────────────────────────────
    csi800 = load_meta("csi800")
    if csi800.empty:
        logger.error("中证800成分股数据缺失")
        return
    codes = set(csi800["code"].tolist())

    if UNIVERSE == "1800":
        csi1000 = load_meta("csi1000")
        if csi1000.empty:
            logger.warning("中证1000数据缺失，仅用CSI 800")
        else:
            codes |= set(csi1000["code"].tolist())

    codes = sorted(codes)
    logger.info(f"股票池: {len(codes)} 只（{UNIVERSE}）")

    logger.info("加载价格+成交额矩阵...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, BACKTEST_END)
    if panel.empty:
        logger.error("价格数据加载失败")
        return

    rebalance_dates = get_monthly_rebalance_dates(trade_calendar)
    logger.info(f"调仓月数: {len(rebalance_dates)}")

    logger.info("运行回测...")
    nav = run_backtest(panel, rebalance_dates, amount_panel)

    logger.info("── 分年度表现 ──")
    for year in range(2019, 2025):
        yn = nav[nav.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:.1%}  最大回撤:{yd:.1%}")

    metrics = calc_metrics(nav)
    logger.info("── 总体指标 ──")
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")

    nav.to_csv(f"logs/backtest_a_nav_{FORMULA}.csv", header=["nav"])
    logger.info(f"净值已保存 → logs/backtest_a_nav_{FORMULA}.csv")

    ar = float(metrics["年化收益率"].strip("%")) / 100
    md = float(metrics["最大回撤"].strip("%")) / 100
    sr = float(metrics["夏普比率"])
    passed = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    logger.info(f"\n回测结论: {'✅ 达标' if passed else '⚠️  未达标'}  "
                f"(目标: 年化≥15% 回撤≥-25% 夏普≥1.0)")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
