"""CB 溢价率计算 (2026-09-29 建, 节后 premium 修复的代码准备)。

背景(2026-09-28 实锤): akshare CB 日线接口仅 OHLCV 无溢价列, 快照构建
`or 0` 兜底 → 全链溢价率恒 0 → dblow=价格单因子(实际口径≠spec"双低")。
修复路径(2026-09-29 验证可行): 溢价率 = CB价格/转换价值 − 1,
转换价值 = 100/转股价 × 正股价——CB 日线、正股日线、转股价均在本地库。

盲区声明(待用户节后裁定 v1/v2):
- 转股价用 cb_universe 现值快照——**有下修历史的券, 下修前时段的溢价率会算错**
  (下修幅度通常 10~30%, 影响该券下修前的排名)。v2=接入下修历史(候选源:
  MCP G2 cb-price-chg / jsl 转股价表), v1=声明盲区先上线。
- 正股停牌日无价 → 当日溢价率 NaN(快照构建时按缺失处理)。
"""
import pandas as pd


def compute_premium(cb_close: pd.Series, stock_close: pd.Series,
                    convert_price: float) -> pd.Series:
    """给定 CB 收盘价序列、正股收盘价序列(同日期索引)、转股价, 返回溢价率(%)。

    cb_close / stock_close 均需 date 索引; 返回与 cb_close 对齐的溢价率序列,
    正股缺价日为 NaN(调用方决定缺失语义)。
    """
    conv_value = 100.0 / convert_price * stock_close.reindex(cb_close.index)
    premium = (cb_close / conv_value - 1.0) * 100.0
    premium = premium.where(cb_close > 0)
    return premium.astype(float)
