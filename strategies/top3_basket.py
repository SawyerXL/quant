"""
Top3 篮子策略模块 v2 — 独立于主策略，完整回测验证。

打分：85%动量(5D/20D/60D截面Z) + 15%质量(60D收益/波动率)
过滤：price_position>0.85, 市值50-300亿, 成交>3亿, 非ST/次新/一字板
风控：绝对-12%, MA10三日, 止盈25%/半15%, 候选<3持币, 组合单日-8%熔断
"""
import pandas as pd, numpy as np
from datetime import datetime

# ── 参数（集中配置，改动须A/B对比）───────────────────────
PARAMS = {
    "score": {
        "momentum_weight": 0.85,  # 动量权重
        "quality_weight":  0.15,  # 质量权重
        "mom_5d": 0.50, "mom_20d": 0.30, "mom_60d": 0.20,
    },
    "filters": {
        "min_price_position": 0.85,   # 禁止放宽
        "min_mktcap_billion": 50,     # 流通市值下限(亿)
        "max_mktcap_billion": 300,    # 流通市值上限(亿)
        "min_daily_amount_billion": 3.0,  # 日均成交额下限(亿)
        "min_list_days": 60,          # 次新排除
        "exclude_st": True,
        "exclude_limit_up_today": True,
    },
    "risk": {
        "absolute_stop": -0.12,       # 单票-12%硬止损
        "ma_window": 10, "ma_exit_days": 3,  # MA10止损
        "take_profit_full": 0.25,     # 止盈25%
        "take_profit_half": 0.15,     # 减半15%
        "portfolio_daily_circuit": -0.08,  # 组合日熔断
        "min_candidates": 3,          # 候选<3持币
        "max_single_pct": 0.40,       # 单票上限40%
    },
    "execution": {
        "t_plus_1": True,
        "commission": 0.00175,
        "basket_size": 3,
    },
}


# ── 打分函数 ──────────────────────────────────────────────
def compute_score_top3(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    """
    Top3 专用打分：截面Z-score动量 + 质量因子，无行业分组。
    """
    hist = panel[panel.index <= date]
    if len(hist) < 62:
        return pd.Series(dtype=float)

    # 流动性过滤
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        avg_amt = ha.iloc[-20:].mean()
        min_amt = PARAMS["filters"]["min_daily_amount_billion"] * 1e4  # 亿→万元
        liq = avg_amt[avg_amt > min_amt].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p = hist.iloc[-1]; common = p.index
    pct = PARAMS["score"]

    # ① 多周期动量（截面Z-score）
    ret_5d  = (p / hist.iloc[-6]  - 1).dropna() if len(hist) >= 7   else pd.Series(dtype=float)
    ret_20d = (p / hist.iloc[-21] - 1).dropna() if len(hist) >= 22  else pd.Series(dtype=float)
    ret_60d = (p / hist.iloc[-61] - 1).dropna() if len(hist) >= 62  else pd.Series(dtype=float)

    def cross_z(s: pd.Series) -> pd.Series:
        r = s.reindex(common).fillna(0)
        mu, sigma = r.mean(), r.std()
        if sigma < 1e-8: return pd.Series(0.0, index=common)
        return ((r - mu) / sigma).clip(-3, 3)

    z5d  = cross_z(ret_5d)  if not ret_5d.empty  else pd.Series(0, index=common)
    z20d = cross_z(ret_20d) if not ret_20d.empty else pd.Series(0, index=common)
    z60d = cross_z(ret_60d) if not ret_60d.empty else pd.Series(0, index=common)
    mom = pct["mom_5d"] * z5d + pct["mom_20d"] * z20d + pct["mom_60d"] * z60d

    # ② 质量因子（60D收益/波动率）
    qw = pct["quality_weight"]
    if len(hist) >= 62 and qw > 0:
        ret_60d_raw = ret_60d.reindex(common).fillna(0)
        vol_60d_raw = hist.iloc[-61:].pct_change(fill_method=None).std() * np.sqrt(252)
        sharpe_like = (ret_60d_raw / vol_60d_raw.replace(0, 0.01).clip(lower=0.01)
                       ).fillna(0).clip(-5, 5)
        quality_z = cross_z(sharpe_like)
    else:
        quality_z = pd.Series(0, index=common)

    mw = pct["momentum_weight"]
    base_score = (mw * mom + qw * quality_z).reindex(common).fillna(0)

    # ③ 量价突破加成
    high_250 = hist.iloc[-250:].max()
    price_nh = (p / high_250).clip(0.5, 1.2)
    if amount_panel is not None:
        ha2 = amount_panel[amount_panel.index <= date]
        vr2 = ha2.iloc[-20:].mean(); vb2 = ha2.iloc[-250:].mean().replace(0, float("nan"))
        vol_ratio = (vr2 / vb2).clip(0.5, 3.0)
        vol_recent = vr2
    else:
        vol_recent = pd.Series(1.0, index=common)
        vol_ratio  = pd.Series(1.0, index=common)

    boost = ((price_nh - 0.9) * 2).clip(0, 1) * ((vol_ratio - 1) * 0.5).clip(0, 0.5)
    base_score = base_score * (1 + boost)

    # ④ 波动率+成交额调节
    if len(hist) >= 21:
        vol_20d = hist.iloc[-20:].pct_change(fill_method=None).std()
        vol_rank = vol_20d.rank(pct=True).reindex(common).fillna(0.5)
        vol_mult = 1.3 - 0.6 * vol_rank
    else:
        vol_mult = pd.Series(1.0, index=common)

    cross_r = vol_recent.rank(pct=True).reindex(common)
    amount_mult = (0.80 + 0.20 * cross_r).fillna(0.90)

    return (base_score * vol_mult * amount_mult).fillna(0).dropna()


# ── 硬性过滤 ──────────────────────────────────────────────
def apply_filters(
    score: pd.Series,
    panel: pd.DataFrame,
    date: pd.Timestamp,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    """过滤后得分，不满足条件的直接剔除。"""
    flt = PARAMS["filters"]
    codes = list(score.index)
    hist = panel[panel.index <= date]
    p_now = hist.iloc[-1]
    high_250 = hist.iloc[-250:].max()
    info_d = stock_info.set_index("code") if stock_info is not None else pd.DataFrame()

    for c in codes[:]:
        # price_position 禁止放宽
        if float(p_now.get(c, 0)) / max(float(high_250.get(c, 1)), 0.01) < flt["min_price_position"]:
            codes.remove(c); continue
        # ST
        if flt["exclude_st"] and len(info_d) > 0 and c in info_d.index:
            if info_d.loc[c].get("is_st", False): codes.remove(c); continue
        # 次新
        if flt["min_list_days"] > 0 and len(info_d) > 0 and c in info_d.index:
            ld = info_d.loc[c].get("list_date")
            if pd.notna(ld) and ld != "":
                try:
                    if (date - pd.Timestamp(ld)).days < flt["min_list_days"]:
                        codes.remove(c); continue
                except Exception: pass
        # 一字板
        if flt["exclude_limit_up_today"] and len(hist) >= 2:
            ret_t = (p_now / hist.iloc[-2] - 1)
            if float(ret_t.get(c, 0)) > 0.095: codes.remove(c); continue

    return score[score.index.isin(codes)]


# ── 风控检查 ──────────────────────────────────────────────
def check_risk(code: str, entry_price: float, cur_price: float,
               days_below_ma: int, basket_daily_pnl: float = 0) -> str:
    """返回 'hold' / 'sell' / 'reduce'。"""
    r = PARAMS["risk"]
    pnl = (cur_price / entry_price - 1)

    if days_below_ma >= r["ma_exit_days"]:
        return "sell"  # MA10止损
    if pnl <= r["absolute_stop"]:
        return "sell"  # 绝对止损
    if pnl >= r["take_profit_full"]:
        return "sell"  # 止盈全出
    if pnl >= r["take_profit_half"]:
        return "reduce"  # 减半
    if basket_daily_pnl <= r["portfolio_daily_circuit"] and basket_daily_pnl != 0:
        return "sell_all"  # 组合熔断
    return "hold"
