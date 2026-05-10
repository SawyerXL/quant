"""
回测引擎：基于 vectorbt 封装，处理 A 股特有规则。
使用方式：见 scripts/backtest_a.py
"""
import pandas as pd
import numpy as np
import vectorbt as vbt
from loguru import logger
from config.settings import (
    BACKTEST_INIT_CAPITAL, COMMISSION_RATE, STAMP_TAX,
    SLIPPAGE, MIN_LOT,
)


# ------------------------------------------------------------------
# A 股特有约束处理
# ------------------------------------------------------------------
def round_to_lot(shares: float) -> int:
    """向下取整到100的整数倍（A股最小单位：1手=100股）。"""
    return int(shares // MIN_LOT) * MIN_LOT


def apply_limit_filter(
    signals: pd.DataFrame,
    limit_up_df: pd.DataFrame,
    limit_down_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    涨停不买，跌停不卖。
    signals: index=date，columns=code，值为目标仓位权重
    limit_up_df / limit_down_df: index=date，columns=code，bool
    返回过滤后的 signals。
    """
    filtered = signals.copy()
    # 涨停时买入信号置零
    if not limit_up_df.empty:
        buy_mask  = (signals > 0) & limit_up_df.reindex_like(signals).fillna(False)
        filtered[buy_mask] = 0
    # 跌停时卖出信号置零（持仓维持原位）
    if not limit_down_df.empty:
        sell_mask = (signals == 0) & limit_down_df.reindex_like(signals).fillna(False)
        filtered[sell_mask] = signals[sell_mask]
    return filtered


# ------------------------------------------------------------------
# 核心回测函数
# ------------------------------------------------------------------
def run_backtest(
    close: pd.DataFrame,
    target_weights: pd.DataFrame,
    init_capital: float = None,
    commission: float = None,
    stamp_tax: float = None,
    slippage: float = None,
    freq: str = "monthly",
) -> dict:
    """
    运行向量化回测。

    close: index=date(datetime)，columns=code，后复权收盘价
    target_weights: index=调仓日(datetime)，columns=code，目标权重（行和≤1）
    freq: 'monthly' | 'weekly' | 'daily'

    返回 dict: {
        'portfolio': vbt.Portfolio 对象,
        'stats': 统计指标 dict,
        'returns': 每日收益率 Series,
    }
    """
    ic = init_capital or BACKTEST_INIT_CAPITAL
    comm = (commission or COMMISSION_RATE) + (stamp_tax or STAMP_TAX) / 2
    slip = slippage or SLIPPAGE

    logger.info(f"开始回测: 资金={ic:,}, 手续费={comm:.4f}, 滑点={slip:.4f}")

    # 将调仓权重对齐到交易日（前向填充）
    weights_daily = target_weights.reindex(close.index, method="ffill").fillna(0)

    # 使用 vectorbt Portfolio.from_orders 方式（更接近实际）
    price_with_slip = close * (1 + slip)   # 买入按滑点加价

    pf = vbt.Portfolio.from_orders(
        close=close,
        size=weights_daily,
        size_type="targetpercent",
        fees=comm,
        slippage=slip,
        init_cash=ic,
        freq="1D",
        call_seq="auto",
    )

    stats = {
        "total_return":     float(pf.total_return()),
        "annual_return":    float(pf.annualized_return()),
        "max_drawdown":     float(pf.max_drawdown()),
        "sharpe_ratio":     float(pf.sharpe_ratio()),
        "calmar_ratio":     float(pf.calmar_ratio()),
        "win_rate":         float(pf.trades.win_rate() if len(pf.trades.records) else 0),
        "total_trades":     int(len(pf.trades.records)),
    }

    logger.info(
        f"回测结果: 年化={stats['annual_return']:.1%}, "
        f"最大回撤={stats['max_drawdown']:.1%}, "
        f"夏普={stats['sharpe_ratio']:.2f}"
    )
    return {
        "portfolio": pf,
        "stats": stats,
        "returns": pf.returns(),
    }


# ------------------------------------------------------------------
# 金标准验证（Week 2 必做）
# ------------------------------------------------------------------
def gold_standard_test(close: pd.DataFrame, trade_calendar: list[str]) -> dict:
    """
    用沪深300等权月度再平衡策略验证回测引擎正确性。
    结果应接近沪深300指数收益（误差<5%），否则引擎有问题。
    """
    from data.source import get_source
    src = get_source()
    components = src.get_index_components("000300", trade_calendar[-1])

    # 找到每月末交易日
    dates = pd.DatetimeIndex(trade_calendar)
    monthly_ends = dates[dates.is_month_end | (dates.month != dates.shift(-1).month)]

    # 等权权重
    n = len(components)
    w = 1.0 / n
    weights = pd.DataFrame(
        {c: w for c in components},
        index=monthly_ends,
    )

    close_300 = close[components].dropna(axis=1, thresh=int(len(close) * 0.8))
    result = run_backtest(close_300, weights)

    # 获取指数收益对比
    try:
        idx = src.get_daily("000300", trade_calendar[0], trade_calendar[-1])
        if not idx.empty:
            idx_ret = (idx.set_index("date")["close"].iloc[-1] /
                       idx.set_index("date")["close"].iloc[0] - 1)
            result["stats"]["index_return_300"] = float(idx_ret)
    except Exception:
        pass

    logger.info(
        f"金标准测试完成: 组合年化={result['stats']['annual_return']:.1%}"
    )
    return result
