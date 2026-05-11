"""
Track A 多因子选股策略回测脚本。
用纯 pandas/numpy 实现，不依赖 vectorbt（避免版本兼容问题）。

覆盖：2019-01-01 至 2024-12-31
策略：中证800成分股，月末选30只等权，次月初开盘执行
因子：动量40% + 质量(ROE)30% + 估值(EP)20% + 净资产(BP)10%

运行：
    python scripts/run_backtest_a.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_financial, load_meta

logger.add("logs/backtest_a.log", rotation="1 day", retention="30 days")

BACKTEST_START = "2019-01-01"
BACKTEST_END   = "2024-12-31"
N_HOLDINGS     = 30
COMMISSION     = 0.00125   # 买入万2.5 + 卖出万2.5+千1印花 ≈ 单边0.125%

# 财务报告公布滞后（季报60天、年报120天保守估计）
REPORT_LAG_DAYS = 60

# 因子权重
W_MOMENTUM = 0.40
W_QUALITY  = 0.30
W_EP       = 0.20
W_BP       = 0.10


def load_price_panel(codes: list, start: str, end: str) -> pd.DataFrame:
    """加载多股票收盘价矩阵，index=date，columns=code。"""
    frames = {}
    for code in codes:
        df = load_daily(code, start, end)
        if df.empty or "close" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"]
        if len(s) > 200:
            frames[code] = pd.to_numeric(s, errors="coerce")
    if not frames:
        return pd.DataFrame()
    panel = pd.DataFrame(frames).sort_index()
    logger.info(f"价格矩阵: {panel.shape[0]} 天 × {panel.shape[1]} 只股票")
    return panel


def load_all_financial(codes: list) -> dict[str, pd.DataFrame]:
    """预加载所有股票财务数据到内存，回测时避免重复 IO。"""
    fin = {}
    for code in codes:
        df = load_financial(code)
        if not df.empty and "report_date" in df.columns:
            df["report_date"] = pd.to_datetime(df["report_date"])
            fin[code] = df.sort_values("report_date").reset_index(drop=True)
    logger.info(f"已加载财务数据: {len(fin)}/{len(codes)} 只")
    return fin


def _get_latest_financial(fin_data: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series | None:
    """取截止 as_of 日期可用的最新季报（含 60 天发布滞后）。"""
    cutoff = as_of - pd.Timedelta(days=REPORT_LAG_DAYS)
    avail = fin_data[fin_data["report_date"] <= cutoff]
    if avail.empty:
        return None
    return avail.iloc[-1]


def _zscore(s: pd.Series) -> pd.Series:
    """截面 z-score 标准化，对异常值做截断（±3σ）。"""
    mu, sigma = s.mean(), s.std()
    if sigma < 1e-8:
        return pd.Series(0.0, index=s.index)
    z = (s - mu) / sigma
    return z.clip(-3, 3)


def compute_combined_score(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    fin_map: dict[str, pd.DataFrame],
) -> pd.Series:
    """
    截面多因子合并得分。
    无财务数据的股票降级为纯动量打分（权重归一化到1.0）。
    """
    hist = panel[panel.index <= date]
    if len(hist) < 250:
        return pd.Series(dtype=float)

    # ── 动量因子 ──────────────────────────────
    ret_20  = hist.iloc[-1] / hist.iloc[-20] - 1
    ret_240 = hist.iloc[-1] / hist.iloc[-240] - 1
    mom = (_zscore(-ret_20) + _zscore(ret_240 - ret_20)) / 2

    # ── 财务因子（截面，只含有数据的股票）──────
    roe_raw, ep_raw, bp_raw = {}, {}, {}
    prices = hist.iloc[-1]

    for code in panel.columns:
        if code not in fin_map:
            continue
        row = _get_latest_financial(fin_map[code], date)
        if row is None:
            continue
        price = prices.get(code)
        if pd.isna(price) or price <= 0:
            continue
        if pd.notna(row.get("roe_annualized")):
            roe_raw[code] = row["roe_annualized"]
        if pd.notna(row.get("eps_ttm")) and row["eps_ttm"] > 0:
            ep_raw[code] = row["eps_ttm"] / price * 100   # EP × 100 量级对齐
        if pd.notna(row.get("bvps")) and row["bvps"] > 0:
            bp_raw[code] = row["bvps"] / price

    roe_z = _zscore(pd.Series(roe_raw)) if roe_raw else pd.Series(dtype=float)
    ep_z  = _zscore(pd.Series(ep_raw))  if ep_raw  else pd.Series(dtype=float)
    bp_z  = _zscore(pd.Series(bp_raw))  if bp_raw  else pd.Series(dtype=float)

    # ── 合并：有财务数据用全权重，无数据纯动量 ─
    fin_codes = set(roe_z.index) | set(ep_z.index) | set(bp_z.index)
    scores = {}
    for code in mom.index:
        if pd.isna(mom[code]):
            continue
        if code in fin_codes:
            q = roe_z.get(code, 0.0)
            e = ep_z.get(code, 0.0)
            b = bp_z.get(code, 0.0)
            scores[code] = W_MOMENTUM * mom[code] + W_QUALITY * q + W_EP * e + W_BP * b
        else:
            scores[code] = mom[code]   # 纯动量兜底

    return pd.Series(scores).dropna()


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
    fin_map: dict[str, pd.DataFrame],
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

        if date_str in rebalance_set and i >= 250:
            score = compute_combined_score(panel, date, fin_map)
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

    roll_max     = nav.cummax()
    drawdown     = (nav - roll_max) / roll_max
    max_drawdown = drawdown.min()

    monthly_nav  = nav.resample("ME").last()
    monthly_rets = monthly_nav.pct_change().dropna()
    win_rate     = (monthly_rets > 0).mean()

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
    logger.info(f"Track A 多因子回测: {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"因子权重: 动量{W_MOMENTUM} 质量{W_QUALITY} EP{W_EP} BP{W_BP}")
    logger.info("=" * 60)

    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失")
        return
    trade_calendar = cal_df["trade_date"].tolist()
    trade_calendar = [d for d in trade_calendar if BACKTEST_START <= d <= BACKTEST_END]

    csi800 = load_meta("csi800")
    if csi800.empty:
        logger.error("中证800成分股数据缺失")
        return
    codes = csi800["code"].tolist()
    logger.info(f"股票池: 中证800，{len(codes)} 只")

    logger.info("加载价格矩阵中...")
    panel = load_price_panel(codes, BACKTEST_START, BACKTEST_END)
    if panel.empty:
        logger.error("价格数据加载失败")
        return

    logger.info("加载财务数据中...")
    fin_map = load_all_financial(codes)

    rebalance_dates = get_monthly_rebalance_dates(trade_calendar)
    logger.info(f"调仓日期数: {len(rebalance_dates)} 个月")

    logger.info("运行多因子回测中...")
    nav = run_backtest(panel, rebalance_dates, fin_map)

    logger.info("── 分年度表现 ──")
    for year in range(2019, 2025):
        year_nav = nav[nav.index.year == year]
        if len(year_nav) < 2:
            continue
        yr = year_nav.iloc[-1] / year_nav.iloc[0] - 1
        yd = ((year_nav - year_nav.cummax()) / year_nav.cummax()).min()
        logger.info(f"  {year}  收益:{yr:.1%}  最大回撤:{yd:.1%}")

    metrics = calc_metrics(nav)
    logger.info("── 总体指标（2019-2024）──")
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")

    result_path = Path("logs/backtest_a_nav.csv")
    nav.to_csv(result_path, header=["nav"])
    logger.info(f"净值序列已保存 → {result_path}")

    ar = float(metrics["年化收益率"].strip("%")) / 100
    md = float(metrics["最大回撤"].strip("%")) / 100
    sr = float(metrics["夏普比率"])
    passed = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    verdict = "✅ 达标（可推进实盘）" if passed else "⚠️  未达标（需调整策略）"
    logger.info(f"\n回测结论: {verdict}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
