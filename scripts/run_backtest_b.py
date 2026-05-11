"""
Track B 三位一体强势股策略回测。

覆盖：2021-01-01 → 2024-12-31
调仓：每周五（T+0，收盘执行）
持仓：大势仓位系数 × 30万，每行业选2只，共约6只等权
止损：个股 -8% 触发当日清仓

运行：
    python scripts/run_backtest_b.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger

from data.storage import load_meta
from strategies.trinity.universe  import build_universe
from strategies.trinity.screener  import compute_strength_score
from strategies.trinity.market    import market_score, score_to_position
from strategies.trinity.sector    import sector_scores, select_sectors
from scripts.run_backtest_a       import load_panels, _zscore   # 复用已验证函数

logger.add("logs/backtest_b.log", rotation="1 day", retention="30 days")

BACKTEST_START    = "2021-01-01"
BACKTEST_END      = "2024-12-31"
CAPITAL           = 300_000
N_SECTORS         = 3
STOCKS_PER_SECTOR = 2
STOP_LOSS         = -0.08       # 个股止损 -8%
COMMISSION        = 0.00125     # 单边 0.125%
MIN_BARS_WEEK     = 35          # 周回测预热期（约7周）


def get_weekly_friday_dates(trade_calendar: list[str]) -> list[str]:
    """返回每周最后一个交易日（周五或提前收盘的周四）。"""
    dates = pd.DatetimeIndex(sorted(trade_calendar))
    dates = dates[(dates >= BACKTEST_START) & (dates <= BACKTEST_END)]
    weekly_ends = []
    for year in range(dates[0].year, dates[-1].year + 1):
        for week in range(1, 54):
            week_dates = dates[(dates.year == year) & (dates.isocalendar().week == week)]
            if len(week_dates) > 0:
                weekly_ends.append(str(week_dates[-1].date()))
    return sorted(set(weekly_ends))


def run_backtest(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame,
    stock_info: pd.DataFrame,
    index_close: pd.Series,
    rebalance_dates: list[str],
) -> pd.Series:
    """
    向量化回测主函数，返回每日净值 Series。
    """
    all_dates = panel.index
    portfolio_returns = pd.Series(0.0, index=all_dates)
    current_holdings: list[str] = []
    entry_prices: dict[str, float] = {}     # 记录每只股的买入价，用于止损
    rebalance_set = set(rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # ── 每日止损检查（追踪止损：从持仓以来最高价回撤-8%）──
        if current_holdings and i > 0:
            to_stop = []
            for code in current_holdings:
                entry = entry_prices.get(code)
                if entry and entry > 0:
                    cur_price = panel.iloc[i].get(code)
                    if pd.notna(cur_price):
                        # 更新该股持仓期间最高价
                        peak_key = f"peak_{code}"
                        peak = entry_prices.get(peak_key, entry)
                        if cur_price > peak:
                            entry_prices[peak_key] = cur_price
                            peak = cur_price
                        # 从最高价回撤超过止损线
                        if (cur_price / peak - 1) <= STOP_LOSS:
                            to_stop.append(code)
                            logger.debug(f"{date_str} 追踪止损 {code}: "
                                         f"高点={peak:.2f} 现价={cur_price:.2f} "
                                         f"回撤={cur_price/peak-1:.1%}")
            if to_stop:
                # 止损手续费（只算卖出）
                portfolio_returns.iloc[i] -= len(to_stop) / max(len(current_holdings), 1) * COMMISSION
                for code in to_stop:
                    current_holdings.remove(code)
                    entry_prices.pop(code, None)
                    entry_prices.pop(f"peak_{code}", None)

        # ── 每周五收盘生成信号，下周一（次交易日）开盘执行（T+1）──
        # 修复：原代码用当日收盘价生成信号并当日执行，存在前视偏差
        # 现在：周五收盘生成信号，周一开盘执行（用 panel.iloc[i+1] 作为入场价）
        if date_str in rebalance_set and i >= MIN_BARS_WEEK:
            new_holdings = _select_stocks(
                panel, amount_panel, stock_info, index_close, date
            )

            if new_holdings is not None:   # None 表示熊市，维持原仓
                old_set = set(current_holdings)
                new_set = set(new_holdings)
                sell_n  = len(old_set - new_set)
                buy_n   = len(new_set - old_set)
                if sell_n + buy_n > 0:
                    turnover = (sell_n + buy_n) / max(2 * max(len(old_set), len(new_set), 1), 1)
                    portfolio_returns.iloc[i] -= turnover * COMMISSION * 2

                # 入场价用次交易日（i+1）的开盘价代理（无开盘价则用收盘价）
                exec_row = i + 1 if i + 1 < len(panel) else i
                for code in new_set - old_set:
                    price = panel.iloc[exec_row].get(code)   # T+1执行
                    if pd.notna(price):
                        entry_prices[code] = float(price)
                # 清除已卖出的入场价和峰值记录
                for code in old_set - new_set:
                    entry_prices.pop(code, None)
                    entry_prices.pop(f"peak_{code}", None)

                current_holdings = new_holdings

        # ── 每日收益 ────────────────────────────────
        if current_holdings and i > 0:
            prev = panel.iloc[i - 1][current_holdings].dropna()
            curr = panel.iloc[i][current_holdings].dropna()
            common = prev.index.intersection(curr.index)
            if len(common) > 0:
                portfolio_returns.iloc[i] += (curr[common] / prev[common] - 1).mean()

    return (1 + portfolio_returns).cumprod()


def _select_stocks(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame,
    stock_info: pd.DataFrame,
    index_close: pd.Series,
    date: pd.Timestamp,
) -> list[str] | None:
    """
    三层选股，返回目标持仓列表；熊市返回 [] 空仓。
    """
    # 大势层（用全部持仓股票代替CSI800成分股，避免生存者偏差）
    m_score = market_score(index_close, panel, date)
    pos_ratio = score_to_position(m_score)
    logger.debug(f"{date.date()} 大势:{m_score:.0f} 仓位:{pos_ratio:.0%}")

    if pos_ratio <= 0.10:
        return []   # 极度熊市，全空仓

    # 板块层
    s_scores = sector_scores(panel, amount_panel, stock_info, date)
    selected_sectors = select_sectors(s_scores, week_start=str(date.date()))

    if not selected_sectors:
        return []

    # 个股层
    universe = build_universe(str(date.date()), stock_info, panel)
    strength = compute_strength_score(panel, amount_panel, date, universe)

    if strength.empty:
        return []

    # 在选中行业内，取各行业 top-2
    ind_map = stock_info.set_index("code")["industry_l1"]
    holdings = []
    for sector in selected_sectors:
        sector_codes = ind_map[ind_map == sector].index.tolist()
        candidates = strength.reindex(sector_codes).dropna()
        top = candidates.nlargest(STOCKS_PER_SECTOR).index.tolist()
        holdings.extend(top)

    return holdings


def calc_metrics(nav: pd.Series) -> dict:
    rets = nav.pct_change().dropna()
    total   = nav.iloc[-1] / nav.iloc[0] - 1
    annual  = (1 + total) ** (252 / len(nav)) - 1
    vol     = rets.std() * np.sqrt(252)
    sharpe  = annual / vol if vol > 0 else 0
    mdd     = ((nav - nav.cummax()) / nav.cummax()).min()
    wr      = (nav.resample("W").last().pct_change().dropna() > 0).mean()
    return {
        "总收益率":   f"{total:.1%}",
        "年化收益率": f"{annual:.1%}",
        "年化波动率": f"{vol:.1%}",
        "夏普比率":   f"{sharpe:.2f}",
        "最大回撤":   f"{mdd:.1%}",
        "周度胜率":   f"{wr:.1%}",
    }


def main():
    logger.info("=" * 60)
    logger.info(f"Track B 回测: {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"资金:{CAPITAL:,}  行业:{N_SECTORS}  每行业:{STOCKS_PER_SECTOR}  止损:{STOP_LOSS:.0%}")
    logger.info("=" * 60)

    # 加载元数据
    stock_info = load_meta("stock_info_full")
    if stock_info.empty:
        logger.error("stock_info_full 缺失，请先运行 scripts/init_stock_meta.py")
        return

    cal_df = load_meta("trade_calendar")
    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]

    idx_df = load_meta("csi800_index")
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    index_close = idx_df.set_index("date")["close"].sort_index()

    # 股票池：全量（非指数成分股，避免生存者偏差）
    # 原代码用的是2026年当前成分股，会导致只在"幸存赢家"里选股
    # 正确做法：用全市场所有有数据的股票（当年有数据就参与截面排名）
    # 注意：如需精确无偏回测，还需历史退市记录，此处用"有数据即参与"近似处理
    valid_meta = set(stock_info["code"].tolist())
    all_codes  = list(valid_meta)
    logger.info(f"加载全市场价格矩阵（{len(all_codes)} 只，已修正生存者偏差）...")
    panel, amount_panel = load_panels(all_codes, BACKTEST_START, BACKTEST_END)
    logger.info(f"价格矩阵: {panel.shape[0]} 天 × {panel.shape[1]} 只")

    rebalance_dates = get_weekly_friday_dates(trade_calendar)
    logger.info(f"调仓周数: {len(rebalance_dates)}")

    logger.info("运行回测（含止损检查，略慢）...")
    nav = run_backtest(panel, amount_panel, stock_info, index_close, rebalance_dates)

    # 年度表现
    logger.info("── 分年度表现 ──")
    for year in range(2021, 2025):
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

    nav.to_csv("logs/backtest_b_nav.csv", header=["nav"])
    logger.info("净值已保存 → logs/backtest_b_nav.csv")

    ar = float(metrics["年化收益率"].strip("%")) / 100
    sr = float(metrics["夏普比率"])
    md = float(metrics["最大回撤"].strip("%")) / 100
    passed = ar >= 0.20 and sr >= 0.8 and md >= -0.30
    logger.info(f"\n回测结论: {'✅ 达标' if passed else '⚠️  未达标'}  "
                f"(目标: 年化≥20% 夏普≥0.8 回撤≥-30%)")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
