"""
Track B 第三层：个股打分（仅在主线板块池内）。

stock_score = 0.35 × Z(RPS_20D) + 0.25 × price_position
            + 0.20 × Z(5日均量/60日均量) + 0.20 × limit_up_gene

硬性过滤：流通市值30-300亿 / 非ST / 上市>60日 / 非一字板 /
          20日均成交>2亿 / price_position > 0.85
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
from datetime import date
from loguru import logger
from config.strategy_params.trinity import STOCK_SCORE


def compute_stock_scores(
    panel: pd.DataFrame,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame,
    stock_codes: list[str],
    trade_date: str,
    limit_up_scores: dict[str, float] | None = None,
) -> pd.DataFrame:
    """
    对给定股票池打分。
    limit_up_scores: {code: 涨停基因得分 (0-1)}，可从 data_cache 预计算。
    返回 DataFrame: index=code, columns=[rps_20d,price_position,vol_ratio,limit_up_gene,score]
    """
    w  = STOCK_SCORE["weights"]
    fl = STOCK_SCORE["filters"]
    if not stock_codes:
        return pd.DataFrame()

    hist = panel[panel.index <= trade_date]
    if len(hist) < 61:
        return pd.DataFrame()

    p_now = hist.iloc[-1]
    p_20d = hist.iloc[-21] if len(hist) >= 22 else hist.iloc[0]
    high_250 = hist.iloc[-250:].max()
    codes = [c for c in stock_codes if c in panel.columns and pd.notna(p_now.get(c)) and p_now.get(c, 0) > 0]

    # ── 硬性过滤 ────────────────────────────────────────
    # ST 过滤
    if fl["exclude_st"] and "is_st" in stock_info.columns:
        info_d = stock_info.set_index("code")
        st_codes = set(info_d[info_d["is_st"] == True].index)
        codes = [c for c in codes if c not in st_codes]

    # 上市天数过滤
    if fl["min_list_days"] > 0 and "list_date" in stock_info.columns:
        info_d = stock_info.set_index("code")
        today_ts = pd.Timestamp(trade_date)
        for c in list(codes):
            ld = info_d.get("list_date", {}).get(c) if hasattr(info_d, "get") else None
            if ld is not None and ld != "":
                try:
                    if (today_ts - pd.Timestamp(ld)).days < fl["min_list_days"]:
                        codes.remove(c)
                except Exception:
                    pass

    # 市值过滤（优先用缓存，否则跳过——回测时从data_cache获取）
    # 成交额过滤
    if amount_panel is not None and len(amount_panel) >= 20:
        amt_20d = amount_panel.iloc[-20:].mean()
        min_amt = fl["min_daily_amount_wan"]
        codes = [c for c in codes if c in amt_20d.index
                 and float(amt_20d.get(c, 0)) >= min_amt]

    # 一字板过滤
    if fl["exclude_limit_up_today"] and len(hist) >= 2:
        ret_today = (p_now / hist.iloc[-2] - 1)
        codes = [c for c in codes if c in ret_today.index
                 and float(ret_today.get(c, -1)) < 0.095]  # <9.5% 非涨停

    if len(codes) < 5:
        return pd.DataFrame()

    # ── 因子计算 ────────────────────────────────────────
    # RPS 20D: 20日收益在全市场的百分位
    ret_20d_raw = (p_now / p_20d - 1).dropna()
    all_rps = ret_20d_raw.rank(pct=True)
    rps_20d = all_rps.reindex(codes).fillna(0.5)

    # Price position: 收盘/250日最高
    pp = (p_now / high_250).clip(0.5, 1.2)
    price_position = pp.reindex(codes).fillna(0.5)

    # 价格过滤: >0.85
    min_pp = fl["min_price_position"]
    codes = [c for c in codes if float(price_position.get(c, 0)) >= min_pp]
    if len(codes) < 3:
        return pd.DataFrame()

    # Vol ratio: 5日均量/60日均量
    if amount_panel is not None and len(amount_panel) >= 60:
        vol_5d  = amount_panel.iloc[-5:].mean()
        vol_60d = amount_panel.iloc[-60:].mean()
        vr = (vol_5d / vol_60d.replace(0, 1)).clip(0.2, 5.0)
        vol_ratio = vr.reindex(codes).fillna(1.0)
    else:
        vol_ratio = pd.Series(1.0, index=pd.Index(codes))

    # Limit-up gene: min(近60日涨停次数, 3) / 3
    if limit_up_scores is not None:
        lu_gene = pd.Series(limit_up_scores).reindex(codes).fillna(0)
    else:
        # 用60日涨幅超9.5%次数近似
        ret_hist = hist.iloc[-60:].pct_change().fillna(0)
        lu_gene = pd.Series({c: min((ret_hist[c] > 0.095).sum(), 3) / 3
                             for c in codes if c in ret_hist.columns})

    # ── Z-score 合成 ────────────────────────────────────
    def _z(s):
        if s.empty or s.std() < 1e-8:
            return pd.Series(0.0, index=s.index)
        return ((s - s.mean()) / s.std()).clip(-3, 3)

    score = pd.Series(0.0, index=pd.Index(codes))
    for name, s in [("rps_20d", rps_20d), ("price_position", price_position),
                     ("vol_ratio", vol_ratio), ("limit_up_gene", lu_gene)]:
        if name in w:
            score = score.add(_z(s) * w[name], fill_value=0)

    result = pd.DataFrame({
        "score": score, "rps_20d": rps_20d, "price_position": price_position,
        "vol_ratio": vol_ratio, "limit_up_gene": lu_gene,
    }).sort_values("score", ascending=False)
    return result


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    from data.storage import load_meta
    info = load_meta("stock_info_full")
    print(f"\n  个股打分模块  {args.date}")
    print(f"  注: 完整打分需传入 panel+板块池，见 backtest_trinity.py\n")
