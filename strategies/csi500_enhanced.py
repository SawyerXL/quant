"""
CSI500 中盘增强策略 v1 — 牛市6只集中, 弱市10只分散, 主策略完整因子
"""
import pandas as pd, numpy as np
from config.strategy_params.trinity import REGIME  # 复用MA200阈值

PARAMS = {
    "universe": "csi500",
    "bull_n": 6,             # 牛市持仓数
    "weak_n": 10,            # 弱市持仓数
    "ma200_bull_threshold": 1.05,  # CSI800/MA200 >此值=牛市
    "commission": 0.00175,
    "capital": 180000,
    "max_single_pct": 0.25,  # 单票上限25%
    "rebalance_freq": "biweekly",
}


def get_position_size(csi800_close: pd.Series, date: pd.Timestamp) -> int:
    """根据大盘环境返回持仓数。"""
    if len(csi800_close) < 200:
        return PARAMS["weak_n"]
    ma200 = csi800_close[csi800_close.index <= date].rolling(200).mean().iloc[-1]
    ratio = float(csi800_close.loc[date]) / float(ma200) if date in csi800_close.index else 1.0
    return PARAMS["bull_n"] if ratio >= PARAMS["ma200_bull_threshold"] else PARAMS["weak_n"]
