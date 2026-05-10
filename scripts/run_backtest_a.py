"""
Track A 多因子选股策略回测脚本。
用纯 pandas/numpy 实现，不依赖 vectorbt（避免版本兼容问题）。

覆盖：2019-01-01 至 2024-12-31
策略：中证800成分股，月末选30只等权，次月初开盘执行
因子：动量（暂用，价值/质量因子需财务数据，后续补充）

运行：
    python scripts/run_backtest_a.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta

logger.add("logs/backtest_a.log", rotation="1 day", retention="30 days")

BACKTEST_START = "2019-01-01"
BACKTEST_END   = "2024-12-31"
N_HOLDINGS     = 30
COMMISSION     = 0.00125   # 买入万2.5 + 卖出万2.5+千1印花 ≈ 单边0.125%


def load_price_panel(codes: list, start: str, end: str) -> pd.DataFrame:
    """加载多股票收盘价矩阵，index=date，columns=code。"""
    frames = {}
    for code in codes:
        df = load_daily(code, start, end)
        if df.empty or "close" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"]
        if len(s) > 200:   # 至少200个交易日才纳入
            frames[code] = pd.to_numeric(s, errors="coerce")
    if not frames:
        return pd.DataFrame()
    panel = pd.DataFrame(frames).sort_index()
    logger.info(f"价格矩阵: {panel.shape[0]} 天 × {panel.shape[1]} 只股票")
    return panel


def compute_momentum_score(panel: pd.DataFrame, date: pd.Timestamp) -> pd.Series:
    """
    截面动量得分（date 当日）：
    - 反转因子：-20日收益率（A股短期反转效应）
    - 动量因子：240日-20日收益率
    等权合成
    """
    hist = panel[panel.index <= date]
    if len(hist) < 250:
        return pd.Series(dtype=float)
    ret_20  = hist.iloc[-1] / hist.iloc[-20] - 1
    ret_240 = hist.iloc[-1] / hist.iloc[-240] - 1
    reversal = -ret_20
    momentum = ret_240 - ret_20
    score = (reversal + momentum) / 2
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


def run_backtest(panel: pd.DataFrame, rebalance_dates: list) -> pd.Series:
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

        # 调仓日：重新选股
        if date_str in rebalance_set and i >= 250:
            score = compute_momentum_score(panel, date)
            if len(score) >= N_HOLDINGS:
                new_holdings = score.nlargest(N_HOLDINGS).index.tolist()
                # 计算换手成本
                if current_holdings:
                    sell_set = set(current_holdings) - set(new_holdings)
                    buy_set  = set(new_holdings) - set(current_holdings)
                    turnover = (len(sell_set) + len(buy_set)) / (2 * N_HOLDINGS)
                    portfolio_returns.iloc[i] -= turnover * COMMISSION * 2
                current_holdings = new_holdings

        # 当日收益
        if current_holdings and i > 0:
            prev_prices = panel.iloc[i - 1][current_holdings].dropna()
            curr_prices = panel.iloc[i][current_holdings].dropna()
            common = prev_prices.index.intersection(curr_prices.index)
            if len(common) > 0:
                daily_rets = (curr_prices[common] / prev_prices[common] - 1)
                portfolio_returns.iloc[i] += daily_rets.mean()

    nav = (1 + portfolio_returns).cumprod()
    return nav


def calc_metrics(nav: pd.Series) -> dict:
    """计算回测指标。"""
    rets = nav.pct_change().dropna()
    annual_factor = 252

    total_return  = nav.iloc[-1] / nav.iloc[0] - 1
    annual_return = (1 + total_return) ** (annual_factor / len(nav)) - 1
    annual_vol    = rets.std() * np.sqrt(annual_factor)
    sharpe        = annual_return / annual_vol if annual_vol > 0 else 0

    roll_max      = nav.cummax()
    drawdown      = (nav - roll_max) / roll_max
    max_drawdown  = drawdown.min()

    # 月度胜率
    monthly_nav   = nav.resample("ME").last()
    monthly_rets  = monthly_nav.pct_change().dropna()
    win_rate      = (monthly_rets > 0).mean()

    return {
        "总收益率":   f"{total_return:.1%}",
        "年化收益率": f"{annual_return:.1%}",
        "年化波动率": f"{annual_vol:.1%}",
        "夏普比率":   f"{sharpe:.2f}",
        "最大回撤":   f"{max_drawdown:.1%}",
        "月度胜率":   f"{win_rate:.1%}",
        "回测天数":   len(nav),
    }


def main():
    logger.info("=" * 60)
    logger.info(f"Track A 回测开始: {BACKTEST_START} → {BACKTEST_END}")
    logger.info("=" * 60)

    # 读取交易日历和股票池
    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失，无法回测")
        return
    trade_calendar = cal_df["trade_date"].tolist()
    trade_calendar = [d for d in trade_calendar if BACKTEST_START <= d <= BACKTEST_END]

    stock_info = load_meta("stock_info")
    if stock_info.empty:
        logger.error("股票基本信息缺失")
        return
    codes = stock_info["code"].tolist()
    logger.info(f"股票池: {len(codes)} 只")

    # 加载价格面板（全量，约需 1-2 分钟）
    logger.info("加载价格矩阵中（约1-2分钟）...")
    panel = load_price_panel(codes, BACKTEST_START, BACKTEST_END)
    if panel.empty:
        logger.error("价格数据加载失败")
        return

    # 月末调仓日期
    rebalance_dates = get_monthly_rebalance_dates(trade_calendar)
    logger.info(f"调仓日期数: {len(rebalance_dates)} 个月")

    # 运行回测
    logger.info("运行回测中...")
    nav = run_backtest(panel, rebalance_dates)

    # 分年度统计
    logger.info("── 分年度表现 ──")
    for year in range(2019, 2025):
        year_nav = nav[nav.index.year == year]
        if len(year_nav) < 2:
            continue
        yr = year_nav.iloc[-1] / year_nav.iloc[0] - 1
        yd = ((year_nav - year_nav.cummax()) / year_nav.cummax()).min()
        logger.info(f"  {year}  收益:{yr:.1%}  最大回撤:{yd:.1%}")

    # 总体指标
    metrics = calc_metrics(nav)
    logger.info("── 总体指标（2019-2024）──")
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")

    # 保存净值序列
    result_path = Path("logs/backtest_a_nav.csv")
    nav.to_csv(result_path, header=["nav"])
    logger.info(f"净值序列已保存 → {result_path}")

    # 达标判断
    ar = float(metrics["年化收益率"].strip("%")) / 100
    md = float(metrics["最大回撤"].strip("%")) / 100
    sr = float(metrics["夏普比率"])
    passed = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    verdict = "✅ 达标（可推进实盘）" if passed else "⚠️  未达标（需调整策略）"
    logger.info(f"\n回测结论: {verdict}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
