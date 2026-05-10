"""
质量因子：ROE_TTM 和毛利率稳定性。
高质量公司的长期超额收益在A股中被充分验证。
"""
import pandas as pd
import numpy as np


def roe_ttm(financial_df: pd.DataFrame) -> pd.Series:
    """
    TTM ROE = TTM净利润 / 平均净资产。
    financial_df 按 code 聚合，需含最近4个季度数据。
    """
    # 假设 financial_df 已包含 net_profit_ttm 和 avg_equity
    result = financial_df["net_profit_ttm"] / financial_df["avg_equity"].replace(0, np.nan)
    return result.rename("roe_ttm")


def gross_margin_stability(financial_df: pd.DataFrame, n_quarters: int = 8) -> pd.Series:
    """
    毛利率稳定性 = 1 / std(过去N个季度毛利率)。
    越稳定的公司得分越高，可以过滤周期股的波动。
    financial_df: 按 code + report_date 展开的财务数据
    """
    def _stability(group):
        if len(group) < 4:
            return np.nan
        margins = group.sort_values("report_date").tail(n_quarters)["gross_margin"]
        std = margins.std()
        return 1.0 / std if std > 1e-6 else np.nan

    result = financial_df.groupby("code").apply(_stability)
    return result.rename("margin_stability")


def compute_quality_factors(financial_df: pd.DataFrame) -> pd.DataFrame:
    """
    批量计算截面质量因子。
    financial_df 包含：code, report_date, net_profit_ttm, avg_equity, gross_margin
    返回：code + roe_ttm + margin_stability
    """
    # TTM ROE：取最新一期
    latest = financial_df.sort_values("report_date").groupby("code").last().reset_index()
    latest["roe_ttm"] = roe_ttm(latest)

    # 毛利率稳定性：需要历史序列
    stability = gross_margin_stability(financial_df).reset_index()
    stability.columns = ["code", "margin_stability"]

    result = latest[["code", "roe_ttm"]].merge(stability, on="code", how="left")
    return result
