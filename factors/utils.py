"""因子工具函数：标准化、中性化、合成。"""
import pandas as pd
import numpy as np


def zscore(s: pd.Series, clip: float = 3.0) -> pd.Series:
    """截面 Z-score 标准化，并裁剪极端值（±3σ）。"""
    mean, std = s.mean(), s.std()
    if std < 1e-8:
        return pd.Series(0.0, index=s.index)
    z = (s - mean) / std
    return z.clip(-clip, clip)


def industry_zscore(factor: pd.Series, industry: pd.Series) -> pd.Series:
    """
    行业内 Z-score 中性化（去除行业影响）。
    factor, industry: 同 index（code）
    """
    df = pd.DataFrame({"factor": factor, "industry": industry})
    def _norm(g):
        return zscore(g["factor"])
    return df.groupby("industry").apply(_norm, include_groups=False).reset_index(level=0, drop=True).rename(factor.name)


def winsorize(s: pd.Series, pct: float = 0.01) -> pd.Series:
    """上下各截去 pct 分位（去极值）。"""
    lo = s.quantile(pct)
    hi = s.quantile(1 - pct)
    return s.clip(lo, hi)


def composite_score(
    factors: dict[str, pd.Series],
    weights: dict[str, float],
) -> pd.Series:
    """
    加权合成因子得分。
    factors: {因子名: 已标准化的截面 Series}
    weights: {因子名: 权重}（权重无需归一，函数内归一）
    """
    total_w = sum(weights.values())
    score = pd.Series(0.0, index=next(iter(factors.values())).index)
    for name, series in factors.items():
        w = weights.get(name, 0) / total_w
        score += series.fillna(0) * w
    return score.rename("composite_score")
