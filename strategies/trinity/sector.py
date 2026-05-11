"""
Track B 板块层：申万一级行业评分 → 选 top-3 行业。

评分维度：
  4周行业动量   60%：行业内股票等权4周收益率均值，截面 z-score
  成交额增速    25%：近4周均额 / 近20周均额，截面 z-score
  MA 强度      15%：行业内股票高于 MA20 的比例
"""
import json
from pathlib import Path

import pandas as pd
from loguru import logger

from config.strategy_params.trinity import STRATEGY_B

_MANUAL_FILE   = Path("data_store/meta/manual_scores_b.json")
_MANUAL_W      = STRATEGY_B["sector_score"]["rhythm_weight"]   # 0.25
_MIN_SCORE     = STRATEGY_B["sector_score"]["min_score"]       # 60
_MAX_SECTORS   = STRATEGY_B["sector_score"]["max_sectors"]     # 3


def sector_scores(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame,
    stock_info: pd.DataFrame,
    date: pd.Timestamp,
) -> pd.Series:
    """
    计算每个申万一级行业的量化得分（0–100）。
    stock_info 需含 code / industry_l1 列。
    """
    hist_p = panel[panel.index <= date]
    hist_a = amount_panel[amount_panel.index <= date]

    if len(hist_p) < 20 or "industry_l1" not in stock_info.columns:
        logger.warning("数据不足，板块得分全部返回50")
        industries = stock_info["industry_l1"].dropna().unique()
        return pd.Series(50.0, index=industries)

    ind_map = stock_info.set_index("code")["industry_l1"].dropna()
    industries = ind_map.unique()

    results = {}
    for ind in industries:
        codes = ind_map[ind_map == ind].index.tolist()
        codes = [c for c in codes if c in hist_p.columns]
        if len(codes) < 3:   # 成分股太少，行业信号不可靠
            continue

        p_ind = hist_p[codes]
        a_ind = hist_a[[c for c in codes if c in hist_a.columns]] if not hist_a.empty else pd.DataFrame()

        # 4周行业动量
        if len(p_ind) >= 21:
            ret4w = (p_ind.iloc[-1] / p_ind.iloc[-21] - 1).mean()
        else:
            ret4w = 0.0

        # 成交额增速（近4周均 / 近20周均）
        if not a_ind.empty and len(a_ind) >= 20:
            recent4  = a_ind.iloc[-20:].mean().mean()   # 近4周（~20交易日）
            base20   = a_ind.iloc[-100:].mean().mean()  # 近20周（~100交易日）
            amt_ratio = (recent4 / base20 - 1) if base20 > 0 else 0.0
        else:
            amt_ratio = 0.0

        # MA20 强度
        if len(p_ind) >= 20:
            ma20  = p_ind.rolling(20).mean().iloc[-1]
            last  = p_ind.iloc[-1]
            ma_pct = (last > ma20).mean()
        else:
            ma_pct = 0.5

        results[ind] = {
            "momentum": ret4w,
            "amt_ratio": amt_ratio,
            "ma_pct": ma_pct,
        }

    if not results:
        return pd.Series(dtype=float)

    df = pd.DataFrame(results).T

    # 截面 z-score 标准化后合并
    def _zscore_col(s):
        mu, std = s.mean(), s.std()
        if std < 1e-8:
            return pd.Series(0.0, index=s.index)
        return ((s - mu) / std).clip(-3, 3)

    mom_z = _zscore_col(df["momentum"])
    amt_z = _zscore_col(df["amt_ratio"])
    ma_z  = _zscore_col(df["ma_pct"])

    # 映射到 0–100
    def _to_score(z):
        return (z + 3) / 6 * 100

    score = (
        0.60 * _to_score(mom_z) +
        0.25 * _to_score(amt_z) +
        0.15 * _to_score(ma_z)
    )
    return score.rename("sector_score")


def select_sectors(
    quant_scores: pd.Series,
    week_start: str = None,
    top_n: int = _MAX_SECTORS,
    min_score: float = _MIN_SCORE,
) -> list[str]:
    """
    融合人工修正后，选出 top_n 个得分 >= min_score 的行业。
    若达不到 top_n，则放宽到有数据的前 top_n 个。
    """
    scores = quant_scores.copy()

    # 人工修正（融合权重 25%）
    overrides = _load_sector_overrides(week_start)
    if overrides:
        for ind, manual_s in overrides.items():
            if ind in scores.index:
                scores[ind] = scores[ind] * (1 - _MANUAL_W) + manual_s * _MANUAL_W

    # 选 top_n，尽量满足 min_score 门槛
    candidates = scores[scores >= min_score].nlargest(top_n)
    if len(candidates) < top_n:
        candidates = scores.nlargest(top_n)   # 放宽门槛兜底

    selected = candidates.index.tolist()
    score_strs = [f"{s:.0f}" if pd.notna(s) else "N/A" for s in candidates.values]
    logger.info(f"板块层选出: {selected}  (得分: {score_strs})")
    return selected


def _load_sector_overrides(week_start: str | None) -> dict[str, float]:
    """读取人工板块修正分数。"""
    if not _MANUAL_FILE.exists():
        return {}
    try:
        data = json.loads(_MANUAL_FILE.read_text(encoding="utf-8"))
        if week_start and data.get("week_start") != week_start:
            return {}
        return data.get("sector_overrides", {})
    except Exception:
        return {}
