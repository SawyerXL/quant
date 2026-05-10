"""
数据清洗：处理 A 股特有的停牌、ST、复权、涨跌停等数据质量问题。
所有函数接受 DataFrame，返回清洗后的 DataFrame，不修改原数据。
"""
import pandas as pd
import numpy as np
from loguru import logger


def clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    """日线数据清洗：去重、排序、填充、过滤明显异常值。"""
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)

    # 过滤明显错误数据（价格≤0、成交量=0但收盘价变动）
    df = df[df["close"] > 0]

    # 标记停牌日（成交量为0）
    df["is_suspended"] = df["volume"] == 0

    # 涨跌幅异常检测（>11%在非一字板情况下可能是数据错误）
    df["pct_chg_check"] = df["close"].pct_change() * 100
    suspicious = (df["pct_chg_check"].abs() > 20) & (~df["is_suspended"])
    if suspicious.any():
        logger.warning(f"发现 {suspicious.sum()} 条涨跌幅>20%的数据，请人工核查")

    return df


def filter_universe(
    df: pd.DataFrame,
    stock_info: pd.DataFrame,
    date: str,
    exclude_st: bool = True,
    exclude_ipo_days: int = 252,
    exclude_suspended_days: int = 60,
    min_mktcap_quantile: float = 0.10,
) -> list[str]:
    """
    根据过滤条件生成可交易股票池。
    df: 当日全市场截面数据（含 code、volume、close 等）
    stock_info: 股票基本信息（含 list_date、is_st、float_mktcap）
    返回通过过滤的股票代码列表。
    """
    date_ts = pd.Timestamp(date)
    info = stock_info.copy()
    info["list_date"] = pd.to_datetime(info["list_date"])
    info["ipo_days"] = (date_ts - info["list_date"]).dt.days

    # 合并截面数据
    merged = df.merge(info[["code", "is_st", "ipo_days", "float_mktcap"]], on="code", how="left")

    # 过滤 ST
    if exclude_st:
        merged = merged[~merged["is_st"].fillna(False)]

    # 过滤新股
    merged = merged[merged["ipo_days"].fillna(0) >= exclude_ipo_days]

    # 过滤停牌（当日成交量为0）
    merged = merged[merged["volume"] > 0]

    # 过滤小市值（最小10%分位）
    cap_threshold = merged["float_mktcap"].quantile(min_mktcap_quantile)
    merged = merged[merged["float_mktcap"] >= cap_threshold]

    return merged["code"].tolist()


def validate_data_completeness(
    codes: list[str], start: str, end: str, trade_calendar: list[str]
) -> dict:
    """
    检查数据完整性，返回缺失情况报告。
    用于 Week 2 的数据校验。
    """
    from data.storage import load_daily

    report = {"total": len(codes), "missing": [], "incomplete": []}
    trade_dates = [d for d in trade_calendar if start <= d <= end]

    for code in codes:
        df = load_daily(code, start, end)
        if df.empty:
            report["missing"].append(code)
        elif len(df) < len(trade_dates) * 0.9:   # 缺失超过10%视为不完整
            report["incomplete"].append({
                "code": code,
                "expected": len(trade_dates),
                "actual": len(df),
            })

    logger.info(
        f"数据完整性检查: 共{report['total']}只, "
        f"缺失{len(report['missing'])}只, "
        f"不完整{len(report['incomplete'])}只"
    )
    return report
