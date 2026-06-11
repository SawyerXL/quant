"""
Track B 第二层：板块强度评分（三位一体 v2）。

评分维度：5日动量(40%)+20日动量(25%)+涨停占比(20%)+成交额比(15%)
板块状态机：candidate → confirmed → peak → exiting
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from loguru import logger
from config.strategy_params.trinity import SECTOR
from strategies.trinity.data_cache import get_sector_members, get_limit_up_down_stats


def _sector_index_ret(panel: pd.DataFrame, sector_map: dict, period: int) -> pd.Series:
    """每个板块等权 N 日收益率。"""
    if len(panel) < period + 1:
        return pd.Series(dtype=float)
    p_start = panel.iloc[-(period + 1)]
    p_end = panel.iloc[-1]
    rets = {}
    for ind, codes in sector_map.items():
        valid = [c for c in codes if c in panel.columns
                 and pd.notna(p_start.get(c)) and pd.notna(p_end.get(c)) and p_start.get(c, 0) > 0]
        if len(valid) < 3:
            continue
        rets[ind] = np.mean([float(p_end[c] / p_start[c] - 1) for c in valid])
    return pd.Series(rets)


def _sector_amount_ratio(amount_panel: pd.DataFrame, sector_map: dict) -> pd.Series:
    """板块成交额占全市场比 / 其60日均值。"""
    if amount_panel is None or len(amount_panel.columns) < 10:
        return pd.Series(dtype=float)
    recent = amount_panel.iloc[-1]
    total_all = float(recent.sum())
    if total_all == 0:
        return pd.Series(dtype=float)
    ratios = {}
    for ind, codes in sector_map.items():
        valid = [c for c in codes if c in recent.index and pd.notna(recent[c])]
        if len(valid) < 3:
            continue
        sec_amt = float(recent[valid].sum())
        hist_sec = amount_panel.tail(60)[valid].sum(axis=1)
        hist_mean = float(hist_sec.mean()) if not hist_sec.empty else sec_amt
        ratio_60d = hist_sec.sum() / len(hist_sec) if len(hist_sec) > 0 else total_all
        if ratio_60d == 0:
            continue
        ratios[ind] = (sec_amt / total_all) / (ratio_60d / total_all) if total_all > 0 else 0
    return pd.Series(ratios)


def _sector_limit_up_ratio(panel: pd.DataFrame, sector_map: dict,
                           trade_date: str) -> pd.Series:
    """板块内涨幅>9.5%占比（近似涨停比）。"""
    if len(panel) < 2:
        return pd.Series(dtype=float)
    ret_1d = (panel.iloc[-1] / panel.iloc[-2] - 1).dropna()
    ratios = {}
    for ind, codes in sector_map.items():
        valid = [c for c in codes if c in ret_1d.index]
        if len(valid) < 3:
            continue
        lu = sum(1 for c in valid if float(ret_1d.get(c, 0)) > 0.095)
        ratios[ind] = lu / len(valid)
    return pd.Series(ratios)


def _zscore(s: pd.Series) -> pd.Series:
    if s.empty or s.std() < 1e-8:
        return pd.Series(0.0, index=s.index)
    return ((s - s.mean()) / s.std()).clip(-3, 3)


def compute_sector_scores(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame,
    trade_date: str,
) -> pd.DataFrame:
    """计算板块得分排名。返回 DataFrame(index=行业名)。"""
    sector_map = get_sector_members(SECTOR["level"])
    if not sector_map:
        logger.warning("板块成分数据为空")
        return pd.DataFrame()

    w = SECTOR["score_weights"]
    moms = {"momentum_5d": _sector_index_ret(panel, sector_map, 5),
            "momentum_20d": _sector_index_ret(panel, sector_map, 20)}
    lu   = _sector_limit_up_ratio(panel, sector_map, trade_date)
    amt  = _sector_amount_ratio(amount_panel, sector_map)

    score = pd.Series(0.0, index=sector_map.keys())
    for name, s in [("momentum_5d", moms["momentum_5d"]),
                     ("momentum_20d", moms["momentum_20d"]),
                     ("limit_up_ratio", lu), ("amount_ratio", amt)]:
        if not s.empty and name in w:
            score = score.add(_zscore(s) * w[name], fill_value=0)

    df = pd.DataFrame({"score": score,
                       "momentum_5d": moms["momentum_5d"],
                       "momentum_20d": moms["momentum_20d"],
                       "limit_up_ratio": lu, "amount_ratio": amt})\
        .sort_values("score", ascending=False)
    return df


def update_sector_state(prev_state: dict | None,
                        current_scores: pd.DataFrame,
                        trade_date: str) -> pd.DataFrame:
    """板块状态机：candidate → confirmed → exiting。"""
    top_n = SECTOR["top_n"]; conf_n = SECTOR["confirm_top_n"]
    exit_n = SECTOR["exit_top_n"]; cd = SECTOR["confirm_days"]
    ed = SECTOR["exit_days"]

    confirm_pool = set(current_scores.head(conf_n).index)
    exit_pool = set(current_scores.tail(max(1, len(current_scores) - exit_n + 1)).index)
    if prev_state is None:
        prev_state = {}

    states = {}
    for ind in current_scores.index:
        pst, pd_ = (prev_state.get(ind, {}) or {}).get("state", "candidate"), \
                    (prev_state.get(ind, {}) or {}).get("days", 0)
        in_conf = ind in confirm_pool
        in_exit = ind in exit_pool

        if in_conf:
            ns = "confirmed" if pst in ("candidate", "confirmed") else "recovering"
            nd = pd_ + 1 if pst in ("candidate", "confirmed") else 1
        elif in_exit:
            ns = "exiting" if pst in ("confirmed", "exiting") else "candidate"
            nd = pd_ + 1 if pst == "exiting" else 1
        else:
            ns = "candidate" if pst != "confirmed" else "peak"
            nd = 0

        if ns == "confirmed" and nd < cd:
            ns = pst
        if ns == "exiting" and nd < ed:
            ns = pst
        states[ind] = {"state": ns, "days": nd}

    current_scores["state"] = [states.get(i, {}).get("state", "candidate")
                               for i in current_scores.index]
    current_scores["days_in_state"] = [states.get(i, {}).get("days", 0)
                                       for i in current_scores.index]
    return current_scores


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    smap = get_sector_members()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    print(f"\n  板块评分模块  {args.date}")
    print(f"  行业数: {len(smap)}")
    print(f"  注: 完整评分需传入 price+amount panel，见 backtest_trinity.py\n")
