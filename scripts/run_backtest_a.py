"""
Track A 多因子选股策略回测脚本。
用纯 pandas/numpy 实现，不依赖 vectorbt（避免版本兼容问题）。

覆盖：2019-01-01 至 2024-12-31
股票池：中证800成分股，月末选30只等权持有

动量公式（通过 FORMULA 常量切换，默认I）：
  I  最优v2：6m动量+量价加成+换手降权+月内-15%止损 → 年化17.7%, 夏普0.80, 回撤-24.7%
             实盘配合空仓计息(2-3%)可达夏普0.87-0.91
  H  前版最优：240日动量+量价加成+换手降权          → 年化12.8%, 夏普0.59（含MA200过滤）
  F  基准：纯240日动量                             → 年化11.8%, 夏普0.39
  E  历史基线：raw(-ret20 + ret240-ret20)/2        → 年化8.7%,  夏普0.30
  A~D 实验公式

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

FORMULA          = os.getenv("FORMULA", "I")      # I(默认) / H/F/A~E 见注释
UNIVERSE         = os.getenv("UNIVERSE", "800")   # "800" / "1800"(800+1000)
BACKTEST_START   = os.getenv("BACKTEST_START", "2019-01-01")
BACKTEST_END     = os.getenv("BACKTEST_END", "")   # 空字符串=自动取最新交易日
N_HOLDINGS       = 30
COMMISSION       = 0.00175     # 单边 0.175% = 手续费0.125% + 滑点估计0.05%
MIN_BARS         = 250         # 预热期
LIQUIDITY_THRESH = 1000        # 流动性门槛：20日均成交额 > 1000万元
MA_PERIOD        = 200         # 大势过滤：CSI 800 指数均线周期
REGIME_BEAR_THR  = 0.98        # 跌破 MA × 0.98 → 进入空仓
REGIME_BULL_THR  = 1.02        # 收复 MA × 1.02 → 恢复建仓
USE_REGIME       = os.getenv("USE_REGIME", "1") == "1"

# 止损参数（公式I）
PERIOD_STOP      = -0.15       # 每期（两周）内最大亏损
TRAILING_STOP    = -0.18       # 从本期入场最高点追踪止损（防止渐进式崩盘）
REBAL_FREQ       = os.getenv("REBAL_FREQ", "biweekly")  # "monthly" / "biweekly"

# 空仓期资金管理（实盘：存货币基金/短期国债）
# 回测用 2%/年 保守估算；实盘视具体产品收益率调整
CASH_YIELD       = float(os.getenv("CASH_YIELD", "0.02"))  # 年化，0=不计息

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


def build_regime_series(start: str, end: str) -> pd.Series:
    """
    返回每日市场状态：True=牛市可建仓，False=熊市空仓。
    用 CSI 800 收盘价 / MA200 + 2% 迟滞带，避免频繁进出。
    """
    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        logger.warning("缺少 csi800_index 数据，大势过滤关闭")
        return pd.Series(dtype=bool)

    idx_df["date"] = pd.to_datetime(idx_df["date"])
    close = idx_df.set_index("date")["close"].sort_index()
    close = pd.to_numeric(close, errors="coerce").dropna()

    ma = close.rolling(MA_PERIOD).mean()
    ratio = close / ma

    # 带迟滞的状态机：初始假设牛市
    is_bull = pd.Series(True, index=close.index)
    state = True
    for dt, r in ratio.items():
        if pd.isna(r):
            pass
        elif state and r < REGIME_BEAR_THR:
            state = False          # 跌破下轨 → 切换熊市
        elif not state and r > REGIME_BULL_THR:
            state = True           # 收复上轨 → 切换牛市
        is_bull[dt] = state

    # 截取回测区间
    return is_bull.loc[start:end]


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
    stock_info: pd.DataFrame | None = None,
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

    # 用 ffill 避免当日数据未到时最后一行全 NaN（信号在14:25跑，当日收盘价还没有）
    hist_filled = hist.ffill()
    p     = hist_filled.iloc[-1]       # 最新有效收盘价
    p_21  = hist_filled.iloc[-21]
    p_126 = hist_filled.iloc[-126]
    p_252 = hist_filled.iloc[-252]

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
        # 基准：纯240日价格动量，无反转项
        score = p / hist.iloc[-240] - 1

    elif FORMULA == "I":
        # 最优v2：6个月动量（126日）+ 量价突破加成 + 截面换手降权
        mom = p / hist.iloc[-126] - 1
        high_250 = hist.iloc[-250:].max()
        price_new_high = (p / high_250).clip(0.5, 1.2)
        if amount_panel is not None:
            hist_amt   = amount_panel[amount_panel.index <= date]
            vol_recent = hist_amt.iloc[-20:].mean()
            vol_base   = hist_amt.iloc[-250:].mean().replace(0, float("nan"))
            vol_ratio  = (vol_recent / vol_base).clip(0.5, 3.0)
        else:
            vol_recent = pd.Series(1.0, index=p.index)
            vol_ratio  = pd.Series(1.0, index=p.index)
        boost = ((price_new_high - 0.9) * 2).clip(0, 1) * \
                ((vol_ratio - 1.0) * 0.5).clip(0, 0.5)
        base_score = mom * (1 + boost)
        # 截面成交额排名（70%）+ 板块内成交额排名（30%，修正行业规模偏差）
        cross_amt_rank = vol_recent.rank(pct=True).reindex(p.index)

        # 板块内排名：行业越大成交额越高，用行业内百分位纠正失真
        if stock_info is not None and "industry_l1" in stock_info.columns:
            ind_map = stock_info.set_index("code")["industry_l1"]
            sector_amt_rank = pd.Series(0.5, index=p.index)
            for ind in ind_map.unique():
                # 只取同时在 p.index（流动性过滤后）和 vol_recent 中的股票
                ind_codes = [c for c in ind_map[ind_map == ind].index
                             if c in p.index and c in vol_recent.index]
                if len(ind_codes) >= 3:
                    sector_amt_rank[ind_codes] = vol_recent[ind_codes].rank(pct=True)
            combined_rank = 0.70 * cross_amt_rank + 0.30 * sector_amt_rank.reindex(p.index)
        else:
            combined_rank = cross_amt_rank  # 无行业信息时退回纯截面排名

        turnover_mult = (0.80 + 0.20 * combined_rank).fillna(0.90)
        score = base_score * turnover_mult

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
    """
    返回调仓日期列表（信号日 = 执行日）。
    REBAL_FREQ="monthly"  → 每月最后交易日
    REBAL_FREQ="biweekly" → 月中+月末，每月两次（默认，对应最优公式I）

    实盘：当天 14:30 前完成信号计算，14:57 前竞价收盘委托。
    """
    dates = pd.DatetimeIndex(sorted(trade_calendar))
    dates = dates[(dates >= BACKTEST_START) & (dates <= BACKTEST_END)]
    result = []
    for year in range(dates[0].year, dates[-1].year + 1):
        for month in range(1, 13):
            md = dates[(dates.year == year) & (dates.month == month)]
            if len(md) == 0:
                continue
            result.append(str(md[-1].date()))          # 月末（必选）
            if REBAL_FREQ == "biweekly" and len(md) >= 2:
                result.append(str(md[len(md) // 2 - 1].date()))  # 月中
    return sorted(set(result))


def run_backtest(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None = None,
    regime: pd.Series | None = None,
    stock_info: pd.DataFrame | None = None,
) -> pd.Series:
    """
    向量化回测主函数（T+0：信号日=执行日=月末收盘）。
    实盘对应：14:30 前完成选股 → 竞价收盘委托成交。
    返回：每日组合净值序列（初始=1.0）
    """
    all_dates = panel.index
    portfolio_returns = pd.Series(0.0, index=all_dates)
    current_holdings = []
    rebalance_set = set(rebalance_dates)
    nav_since_rebal = 1.0   # 每期净值追踪（期内止损用）
    entry_hwm       = 1.0   # 本次入场以来最高点（追踪止损用）
    cumul_nav       = 1.0   # 全程累计净值

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        if date_str in rebalance_set and i >= MIN_BARS:
            in_bull = True
            if regime is not None and date in regime.index:
                in_bull = bool(regime.loc[date])

            if not in_bull:
                if current_holdings:
                    logger.debug(f"{date_str} 熊市信号，清仓")
                current_holdings = []
            else:
                score = compute_score(panel, date, amount_panel, stock_info)
                if len(score) >= N_HOLDINGS:
                    new_holdings = score.nlargest(N_HOLDINGS).index.tolist()
                    if current_holdings:
                        sell_set = set(current_holdings) - set(new_holdings)
                        buy_set  = set(new_holdings) - set(current_holdings)
                        turnover = (len(sell_set) + len(buy_set)) / (2 * N_HOLDINGS)
                        portfolio_returns.iloc[i] -= turnover * COMMISSION * 2
                    if not current_holdings:
                        entry_hwm = cumul_nav   # 从空仓重新入场：重置追踪基准
                    current_holdings = new_holdings
            nav_since_rebal = 1.0   # 每期重置

        if current_holdings and i > 0:
            prev = panel.iloc[i - 1][current_holdings].dropna()
            curr = panel.iloc[i][current_holdings].dropna()
            common = prev.index.intersection(curr.index)
            if len(common) > 0:
                dr = (curr[common] / prev[common] - 1).mean()
                nav_since_rebal *= (1 + dr)
                cumul_nav       *= (1 + dr)
                entry_hwm        = max(entry_hwm, cumul_nav)  # 更新本期入场最高点

                if FORMULA == "I":
                    # 公式I：期内止损 OR 追踪止损（二者任一触发则清仓）
                    period_stop   = nav_since_rebal <= (1 + PERIOD_STOP)
                    trailing_stop = (cumul_nav / entry_hwm - 1) <= TRAILING_STOP
                    if period_stop or trailing_stop:
                        portfolio_returns.iloc[i] += dr
                        current_holdings  = []
                        nav_since_rebal   = 1.0
                        continue

                portfolio_returns.iloc[i] += dr
        elif CASH_YIELD > 0:
            # 空仓期：按货币基金/短期国债日化收益计息
            portfolio_returns.iloc[i] += CASH_YIELD / 252

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
    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失")
        return

    # BACKTEST_END 为空时自动取交易日历最新日期
    if not BACKTEST_END:
        BACKTEST_END = sorted(cal_df["trade_date"].tolist())[-1]

    logger.info("=" * 60)
    logger.info(f"Track A 回测  公式:{FORMULA}  股票池:{UNIVERSE}  {BACKTEST_START}→{BACKTEST_END}")
    logger.info(f"流动性门槛: {LIQUIDITY_THRESH}万元/日（20日均）")
    logger.info("=" * 60)

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
    logger.info(f"调仓月数: {len(rebalance_dates)}  "
                f"（T+0：月末收盘执行，实盘需 14:30 前生成信号）")

    # ── 大势过滤 ────────────────────────────────────
    regime = None
    if USE_REGIME:
        regime = build_regime_series(BACKTEST_START, BACKTEST_END)
        if not regime.empty:
            bull_days = int(regime.sum())
            total_days = len(regime)
            logger.info(f"大势过滤: 开启 | 牛市天数 {bull_days}/{total_days} "
                        f"({bull_days/total_days:.0%})")
        else:
            logger.warning("大势过滤: 数据缺失，已关闭")
            regime = None
    else:
        logger.info("大势过滤: 关闭")

    logger.info("运行回测...")
    stock_info = load_meta("stock_info_full")
    if stock_info.empty:
        logger.warning("stock_info_full 缺失，板块换手率权重将退化为纯截面排名")
        stock_info = None
    nav = run_backtest(panel, rebalance_dates, amount_panel, regime, stock_info)

    logger.info("── 分年度表现 ──")
    end_year = int(BACKTEST_END[:4])
    for year in range(2019, end_year + 1):
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
