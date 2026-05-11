"""
Track B 强势股三维复合评分。

三个维度（软性 OR，用加权评分替代硬过滤）：
  资金强度 40%：近5日均成交额的全市场百分位排名
  价格强度 30%：近5日（周）涨跌幅 z-score
  趋势强度 30%：MA20>MA30（向上发散） + RPS（相对强度，240日收益百分位）各半

评分范围 0–100（百分位化），越高越强势。
"""
import pandas as pd
import numpy as np
from loguru import logger


def calc_rps(panel: pd.DataFrame, date: pd.Timestamp, window: int = 240) -> pd.Series:
    """
    Relative Price Strength（相对强度指标）。
    返回各股 window 日收益率在全市场的百分位排名（0–100）。
    """
    hist = panel[panel.index <= date]
    if len(hist) < window + 1:
        return pd.Series(dtype=float)
    ret = hist.iloc[-1] / hist.iloc[-window] - 1
    return ret.rank(pct=True).mul(100).rename("rps")


def compute_strength_score(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame,
    date: pd.Timestamp,
    universe: list[str],
) -> pd.Series:
    """
    返回 universe 内每只股票的综合强势得分（0–100）。

    资金强度 40%：近5日均成交额的全市场百分位
    价格强度 30%：近5日涨跌幅 z-score → 百分位
    趋势强度 30%：(MA发散信号 + RPS) / 2 → 百分位
    """
    if not universe:
        return pd.Series(dtype=float)

    hist_p = panel[panel.index <= date]
    hist_a = amount_panel[amount_panel.index <= date]

    if len(hist_p) < 35:
        return pd.Series(dtype=float)

    uni = [c for c in universe if c in hist_p.columns]
    if not uni:
        return pd.Series(dtype=float)

    p = hist_p[uni]

    # ── 资金强度：近5日均成交额百分位 ──────────────
    amt = hist_a[uni] if set(uni).issubset(hist_a.columns) else pd.DataFrame(index=hist_p.index, columns=uni)
    amt_5d = amt.iloc[-5:].mean()
    capital_rank = amt_5d.rank(pct=True).mul(100)   # 0–100

    # ── 价格强度：近5日涨跌幅 ──────────────────────
    if len(p) >= 6:
        ret_5d = p.iloc[-1] / p.iloc[-5] - 1
    else:
        ret_5d = pd.Series(0.0, index=uni)

    # z-score → 百分位映射
    price_rank = ret_5d.rank(pct=True).mul(100)

    # ── 趋势强度：MA 发散 + RPS ────────────────────
    # MA 发散：MA20 > MA30
    ma20 = p.rolling(20).mean().iloc[-1]
    ma30 = p.rolling(30).mean().iloc[-1]
    ma_signal = (ma20 > ma30).astype(float).mul(100)   # 1→100, 0→0

    # RPS：全市场（用传入的完整 panel，不只是 universe）
    rps = calc_rps(panel, date, window=240).reindex(uni).fillna(50)

    trend_score = (ma_signal + rps) / 2

    # ── 合并 ──────────────────────────────────────
    score = (
        0.40 * capital_rank.reindex(uni).fillna(50) +
        0.30 * price_rank.reindex(uni).fillna(50) +
        0.30 * trend_score.reindex(uni).fillna(50)
    )

    return score.dropna().rename("strength_score")
