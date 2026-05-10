"""
动量/反转因子。
A股短期（20日）有显著反转效应，中期（240-20日）有动量效应。
Track A 和 Track B 均使用此因子库。
"""
import pandas as pd
import numpy as np


def reversal_20d(close_df: pd.DataFrame) -> pd.Series:
    """
    短期反转 = -(过去20日收益率)。A股短期反转效应强，取负使因子方向一致（高分=强势）。
    close_df: index=date，columns=code，值为后复权收盘价
    返回：截面因子值（以最新日期的截面为准）
    """
    ret_20 = close_df.pct_change(20).iloc[-1]
    result = -ret_20
    return result.rename("reversal_20d")


def momentum_240d_minus_20d(close_df: pd.DataFrame) -> pd.Series:
    """
    中期动量 = 过去240日收益率 - 过去20日收益率（去除短期反转噪音）。
    """
    ret_240 = close_df.pct_change(240).iloc[-1]
    ret_20  = close_df.pct_change(20).iloc[-1]
    result  = ret_240 - ret_20
    return result.rename("momentum_240d_minus_20d")


def relative_strength(close_df: pd.DataFrame, benchmark: pd.Series, n: int = 20) -> pd.Series:
    """
    个股相对大盘的超额收益（Track B 用于个股打分）。
    close_df: index=date，columns=code
    benchmark: index=date，大盘指数收盘价
    """
    stock_ret    = close_df.pct_change(n).iloc[-1]
    benchmark_ret = benchmark.pct_change(n).iloc[-1]
    return (stock_ret - benchmark_ret).rename(f"rs_{n}d")


def compute_momentum_factors(close_df: pd.DataFrame) -> pd.DataFrame:
    """
    批量计算截面动量因子。
    close_df: index=date（至少需要240+个交易日的历史），columns=code
    返回：code + reversal_20d + momentum_240d_minus_20d
    """
    rev  = reversal_20d(close_df)
    mom  = momentum_240d_minus_20d(close_df)
    result = pd.DataFrame({"reversal_20d": rev, "momentum_240d_minus_20d": mom})
    result.index.name = "code"
    return result.reset_index()
