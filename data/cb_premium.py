"""CB 溢价率计算 (2026-09-29 建; **同日用户裁定: 修复否决, price-only 定案**)。

本文件留档(闸门九: 记元数据不毁数据)——记录修复方案的计算路径与否决理由,
**不得接入快照构建**。裁定依据三条:
① 持仓 30% 有下修史(MCP G2 实测 6/20)——v1 用现值转股价, 下修前历史段
   溢价系统性算错(恒0是已知无信息, 算错是未知有偏);
② 偏误方向乐观: 下修→转换价值被高估→历史溢价被低估→dblow 更"便宜"→
   回测超配下修券=伪alpha(与 V2 未来信息同族);
③ 需引正股不复权价(库内 qfq 不可用)=CB 腿依赖股票日线库, 违反架构原则
   (CLAUDE.md 2026-09-29)。

背景(2026-09-28 实锤): akshare CB 日线接口仅 OHLCV 无溢价列, 快照构建
`or 0` 兜底 → 全链溢价率恒 0 → dblow=价格单因子(实际口径≠spec"双低",
措辞已订正)。修复路径(验证可行但否决): 溢价率 = CB价格/转换价值 − 1,
转换价值 = 100/转股价 × 正股价。
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
