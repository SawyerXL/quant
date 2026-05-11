"""
Track B 大势层：量化打分 → 仓位系数。

得分 0–100，映射到仓位系数 0.1–0.9。
实盘可叠加人工调整（读 manual_scores_b.json）。
"""
import json
from pathlib import Path

import pandas as pd
from loguru import logger

from config.strategy_params.trinity import STRATEGY_B

_MANUAL_FILE = Path("data_store/meta/manual_scores_b.json")
_POS_MAP = STRATEGY_B["market_score"]["position_map"]   # {(lo,hi): ratio}
_MANUAL_W = STRATEGY_B["market_score"]["manual_weight"]  # 0.30


def market_score(
    index_close: pd.Series,
    price_panel: pd.DataFrame,
    date: pd.Timestamp,
) -> float:
    """
    量化打分，0–100。

    MA200 状态    40分：指数/MA200 线性映射（0.98→0, 1.02→40, 中间插值）
    市场宽度      30分：CSI 800 中高于 MA20 的占比 × 30
    近20日涨跌    30分：指数近20日收益 → 百分位 × 30（用2年滚动窗口）
    """
    hist_idx = index_close[index_close.index <= date].dropna()
    if len(hist_idx) < 200:
        logger.warning("指数数据不足200条，大势得分默认50")
        return 50.0

    # MA200 得分
    ma200 = hist_idx.rolling(200).mean().iloc[-1]
    ratio = hist_idx.iloc[-1] / ma200
    if ratio >= 1.02:
        ma_score = 40.0
    elif ratio <= 0.98:
        ma_score = 0.0
    else:
        ma_score = (ratio - 0.98) / (1.02 - 0.98) * 40.0

    # 市场宽度得分（price_panel = CSI 800 收盘价矩阵）
    hist_panel = price_panel[price_panel.index <= date]
    if len(hist_panel) >= 20:
        ma20_panel = hist_panel.rolling(20).mean().iloc[-1]
        last_price = hist_panel.iloc[-1]
        breadth_pct = (last_price > ma20_panel).mean()  # 0–1
    else:
        breadth_pct = 0.5
    breadth_score = breadth_pct * 30.0

    # 近20日涨跌得分
    if len(hist_idx) >= 21:
        ret20 = hist_idx.iloc[-1] / hist_idx.iloc[-21] - 1
        # 用过去2年同样的20日收益率做百分位
        rets = hist_idx.pct_change(20).dropna()
        pct = (rets < ret20).mean()
        trend_score = pct * 30.0
    else:
        trend_score = 15.0

    total = ma_score + breadth_score + trend_score

    logger.debug(f"大势得分: {total:.1f}  "
                 f"MA200={ma_score:.1f} 宽度={breadth_score:.1f} 涨跌={trend_score:.1f}")
    return float(total)


def score_to_position(quant_score: float, week_start: str = None) -> float:
    """
    量化得分 + 人工修正 → 仓位系数。
    若 manual_scores_b.json 存在且 week_start 匹配，则按 30% 权重融合人工打分。
    """
    manual_score = _load_manual_score(week_start)
    if manual_score is not None:
        final = quant_score * (1 - _MANUAL_W) + manual_score * _MANUAL_W
        logger.info(f"大势得分: 量化={quant_score:.0f} 人工={manual_score:.0f} 融合={final:.0f}")
    else:
        final = quant_score

    # 查仓位映射表
    for (lo, hi), ratio in _POS_MAP.items():
        if lo <= final < hi:
            return ratio
    # 边界处理
    return 0.10 if final < 25 else 0.90


def _load_manual_score(week_start: str | None) -> float | None:
    """读取人工打分文件，匹配 week_start；不存在或过期则返回 None。"""
    if not _MANUAL_FILE.exists():
        return None
    try:
        data = json.loads(_MANUAL_FILE.read_text(encoding="utf-8"))
        if week_start and data.get("week_start") != week_start:
            return None
        return float(data["market_manual_score"]) if data.get("market_manual_score") is not None else None
    except Exception:
        return None
