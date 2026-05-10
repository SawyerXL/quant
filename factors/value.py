"""
估值因子：BP（账面市值比）和 EP（盈利市值比）。
A股有效因子，低估值+高盈利具有长期超额收益。
"""
import pandas as pd
import numpy as np


def bp(price: pd.Series, book_value_per_share: pd.Series) -> pd.Series:
    """
    BP = 每股净资产 / 股价。越大表示越被低估。
    price: 当前股价（后复权，截面数据需统一用同一复权方式）
    book_value_per_share: 最新季报每股净资产
    """
    result = book_value_per_share / price.replace(0, np.nan)
    return result.rename("bp")


def ep(price: pd.Series, eps_ttm: pd.Series) -> pd.Series:
    """
    EP = TTM每股收益 / 股价 = 1/PE_TTM。越大表示盈利能力越强（相对价格）。
    eps_ttm: 过去12个月每股收益（滚动TTM）
    """
    result = eps_ttm / price.replace(0, np.nan)
    return result.rename("ep")


def compute_value_factors(cross_section: pd.DataFrame) -> pd.DataFrame:
    """
    批量计算截面估值因子。
    cross_section 必须包含列：code, close, book_value_per_share, eps_ttm
    返回：code + bp + ep
    """
    df = cross_section.copy()
    df["bp"] = bp(df["close"], df["book_value_per_share"])
    df["ep"] = ep(df["close"], df["eps_ttm"])
    return df[["code", "bp", "ep"]]
