"""
情绪/微观结构因子（Track B 专用）。
主要用于 G_score 的资金分、板块内地位计算。
"""
import pandas as pd
import numpy as np


def limit_up_bid_ratio(limit_up_df: pd.DataFrame, daily_df: pd.DataFrame) -> pd.Series:
    """
    涨停封单比 = 涨停封单金额 / 流通市值。
    封单比越高说明追涨资金越坚定，是强势股特征。
    """
    merged = limit_up_df.merge(daily_df[["code", "float_mktcap"]], on="code", how="left")
    result = merged["bid_amount"] / merged["float_mktcap"].replace(0, np.nan)
    result.index = merged["code"]
    return result.rename("limit_up_bid_ratio")


def sector_rank_score(
    daily_df: pd.DataFrame,
    sector_col: str = "sector",
    rank_cols: list[str] = None,
) -> pd.Series:
    """
    量化版"最票"得分：在板块内对多个维度排名并加权合成。
    rank_cols: 需要排名的列（涨幅、连板数、成交额等），越大越好。
    """
    if rank_cols is None:
        rank_cols = ["pct_chg", "consecutive_days", "amount"]

    df = daily_df.copy()
    score = pd.Series(0.0, index=df.index)
    weights = {"pct_chg": 0.40, "consecutive_days": 0.35, "amount": 0.25}

    for col in rank_cols:
        if col not in df.columns:
            continue
        w = weights.get(col, 1.0 / len(rank_cols))
        # 板块内百分位排名
        df[f"rank_{col}"] = df.groupby(sector_col)[col].rank(pct=True)
        score += df[f"rank_{col}"].fillna(0.5) * w

    score.index = df["code"]
    return score.rename("sector_rank_score")


def capital_flow_score(capital_flow_df: pd.DataFrame) -> pd.Series:
    """
    资金流向综合得分（主力净流入 + 超大单净流入）。
    """
    df = capital_flow_df.copy()
    score = df["main_net"].fillna(0) * 0.6 + df["big_net"].fillna(0) * 0.4
    score.index = df["code"]
    return score.rename("capital_flow_score")
