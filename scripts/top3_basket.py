"""
手动跟单篮子：5万本金，始终持有量化模型打分最高的3只股票。
使用独立的高弹性打分公式（更偏短期动量，不做行业平滑），适合小资金激进跟单。

用法：
    python scripts/top3_basket.py           # 查看当前篮子和信号
    python scripts/top3_basket.py --reset   # 重置篮子
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from datetime import date, timedelta
from loguru import logger
from data.storage import load_meta, load_daily
from run_backtest_a2 import ind_zscore
from run_backtest_a import load_panels, MIN_BARS, LIQUIDITY_THRESH

BASKET_SIZE   = 3
CAPITAL       = 50_000
BASKET_FILE   = Path("logs/top3_basket.json")

# ── 高弹性打分（独立于主策略）────────────────────────────────────────
def compute_score_top3(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    """
    Top3篮子专用打分：更偏短期动量，不做行业Z-score平滑。
    核心差异 vs 主策略compute_score_a2：
      - 短周期动量权重↑(1M 50%, 6M 30%, 12M 20%)
      - 截面Z-score而非行业内（允许整个热门行业的股票一起高分）
      - 质量因子↓至15%（高弹性不惩罚波动）
      - 无EPS（不需要基本面拖后腿）
    """
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    # 流动性过滤
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        ra = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p = hist.iloc[-1]
    common = p.index

    # ① 多周期动量（截面Z-score，不做行业内平滑）
    # 超短周期：5日/20日/60日 —— 捕捉亨通光电式爆炸启动
    ret_5d  = (p / hist.iloc[-6]  - 1).dropna() if len(hist) >= 7   else pd.Series(dtype=float)
    ret_20d = (p / hist.iloc[-21] - 1).dropna() if len(hist) >= 22  else pd.Series(dtype=float)
    ret_60d = (p / hist.iloc[-61] - 1).dropna() if len(hist) >= 62  else pd.Series(dtype=float)

    # 截面Z-score：整个市场排名，不按行业分组
    def cross_zscore(s: pd.Series) -> pd.Series:
        r = s.reindex(common).fillna(0)
        mu, sigma = r.mean(), r.std()
        if sigma < 1e-8: return pd.Series(0.0, index=common)
        return ((r - mu) / sigma).clip(-3, 3)

    z5d  = cross_zscore(ret_5d)  if not ret_5d.empty  else pd.Series(0, index=common)
    z20d = cross_zscore(ret_20d) if not ret_20d.empty else pd.Series(0, index=common)
    z60d = cross_zscore(ret_60d) if not ret_60d.empty else pd.Series(0, index=common)

    # 超短周期主导：5D 50% + 20D 30% + 60D 20%
    mom_composite = 0.50 * z5d + 0.30 * z20d + 0.20 * z60d

    # ② 量价突破加成（同主策略）
    high_250 = hist.iloc[-250:].max()
    price_nh = (p / high_250).clip(0.5, 1.2)
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        vr = ha.iloc[-20:].mean()
        vb = ha.iloc[-250:].mean().replace(0, float("nan"))
        vol_ratio  = (vr / vb).clip(0.5, 3.0)
        vol_recent = vr
    else:
        vol_recent = pd.Series(1.0, index=p.index)
        vol_ratio  = pd.Series(1.0, index=p.index)

    boost = ((price_nh - 0.9) * 2).clip(0, 1) * ((vol_ratio - 1) * 0.5).clip(0, 0.5)
    mom_score = mom_composite.reindex(p.index).fillna(0) * (1 + boost)

    # ③ 质量因子轻量化（15%，不做行业分组）
    q_weight = 0.15  # 主策略30%的一半
    if len(hist) >= 62:
        ret_60d_raw = ret_60d.reindex(common).fillna(0)
        vol_60d_raw = hist.iloc[-61:].pct_change(fill_method=None).std() * np.sqrt(252)
        sharpe_like = (ret_60d_raw / vol_60d_raw.replace(0, 0.01).clip(lower=0.01)
                       ).fillna(0).clip(-5, 5).reindex(common)
        quality_z = cross_zscore(sharpe_like) if not sharpe_like.empty else pd.Series(0, index=common)
    else:
        quality_z = pd.Series(0, index=common)

    # 动量85% + 质量15%：高弹性，不拖后腿
    quality_safe = quality_z.reindex(p.index).fillna(0)
    mom_safe     = mom_score.reindex(p.index).fillna(0)
    base_score   = ((1 - q_weight) * mom_safe + q_weight * quality_safe).fillna(0)

    # ④ 波动率+成交额调节（同主策略）
    if len(hist) >= 21:
        vol_20d  = hist.iloc[-20:].pct_change(fill_method=None).std()
        vol_rank = vol_20d.rank(pct=True).reindex(p.index).fillna(0.5)
        vol_mult = 1.3 - 0.6 * vol_rank
    else:
        vol_mult = pd.Series(1.0, index=p.index)

    cross_rank = vol_recent.rank(pct=True).reindex(p.index)
    sec_rank   = pd.Series(0.5, index=p.index)
    if stock_info is not None and "industry_l1" in stock_info.columns:
        ind_map = stock_info.set_index("code")["industry_l1"].reindex(p.index)
        for ind in ind_map.unique():
            ic = [c for c in ind_map[ind_map == ind].index if c in p.index and c in vol_recent.index]
            if len(ic) >= 3: sec_rank[ic] = vol_recent[ic].rank(pct=True)
    combined    = 0.70 * cross_rank + 0.30 * sec_rank.reindex(p.index)
    amount_mult = (0.80 + 0.20 * combined).fillna(0.90)

    return (base_score * vol_mult * amount_mult).dropna()

BASKET_SIZE   = 3
CAPITAL       = 50_000          # 5万本金
BASKET_FILE   = Path("logs/top3_basket.json")

# 加载/保存篮子（含成本价和股数）
def _load_basket() -> dict:
    if BASKET_FILE.exists():
        return json.loads(BASKET_FILE.read_text())
    return {"holdings": [], "cost_prices": {}, "shares": {}, "capital": CAPITAL}

def _save_basket(data: dict):
    BASKET_FILE.parent.mkdir(exist_ok=True)
    data["updated"] = date.today().strftime("%Y-%m-%d")
    BASKET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    today = date.today().strftime("%Y-%m-%d")
    info = load_meta("stock_info_full")
    info["code"] = info["code"].astype(str).str.zfill(6)
    nmap = dict(zip(info["code"], info["name"]))

    # ── 1. 加载当前持仓和打分 ──────────────────────────
    sig_path = Path("data_store/meta/signal_a_latest.json")
    sig = json.loads(sig_path.read_text("utf-8")) if sig_path.exists() else {}
    strategy_holdings = set(sig.get("holdings", []))
    strategy_sell     = set(sig.get("sell", []))
    ma10_exits        = set(sig.get("ma10_exits", []))

    # 用策略同款打分算全市场排名
    csi = load_meta("csi800")
    codes = sorted(csi["code"].tolist())
    panel, ap = load_panels(codes, "2025-06-01", today)
    score = compute_score_top3(panel, pd.Timestamp(today), ap, info)

    # ── 2. 当前篮子 ──────────────────────────────────
    data   = _load_basket()
    basket = data["holdings"]
    cost_p = data.get("cost_prices", {})
    shares = data.get("shares", {})
    cash   = data.get("capital", CAPITAL)

    if not basket:
        # 首次：取全市场得分最高的3只（不限于策略持仓），资金三等分
        top3 = score.nlargest(BASKET_SIZE)
        basket = top3.index.tolist()
        per_stock = CAPITAL / BASKET_SIZE
        for code in basket:
            df = load_daily(code, (date.today()-timedelta(days=5)).strftime("%Y-%m-%d"), today)
            cur = float(df["close"].iloc[-1]) if not df.empty else score.get(code, 0)
            lot = 100
            qty = max(int(per_stock / cur / lot) * lot, lot)
            cost_p[code] = cur
            shares[code] = qty
        data = {"holdings": basket, "cost_prices": cost_p, "shares": shares, "capital": CAPITAL}
        _save_basket(data)

    # ── 3. 检查是否需要换仓 ──────────────────────────
    to_remove = []
    for code in basket:
        if code in ma10_exits:
            to_remove.append((code, "策略MA10止损"))
        elif code in strategy_sell:
            to_remove.append((code, "策略调仓卖出"))
        elif code not in strategy_holdings:
            to_remove.append((code, "已不在策略持仓"))

    # ── 4. 选替补 ────────────────────────────────────
    candidates = score.nlargest(30).index.tolist()
    for old_code, reason in to_remove:
        for c in candidates:
            if c not in basket and c not in [r[0] for r in to_remove if r != (old_code, reason)]:
                df = load_daily(c, (date.today()-timedelta(days=5)).strftime("%Y-%m-%d"), today)
                cur = float(df["close"].iloc[-1]) if not df.empty else 0
                lot = 100
                per_stock = CAPITAL / BASKET_SIZE
                qty = max(int(per_stock / cur / lot) * lot, lot)
                cost_p[c] = cur
                shares[c] = qty
                basket.remove(old_code)
                basket.append(c)
                cost_p.pop(old_code, None)
                shares.pop(old_code, None)
                print(f"\n  ⚠️  替换: 卖出 {old_code} {nmap.get(old_code,'?')}（{reason}）"
                      f" → 买入 {c} {nmap.get(c,'?')} {qty}股 @{cur:.2f}")
                try:
                    from monitoring.alerts import send_alert
                    send_alert(
                        f"【Top3篮子调仓】{today}\n"
                        f"卖出: {old_code} {nmap.get(old_code,'?')}（{reason}）\n"
                        f"买入: {c} {nmap.get(c,'?')} {qty}股 @{cur:.2f}\n"
                        f"篮子: {', '.join(basket[:3])}"
                    )
                except Exception: pass
                break
    data = {"holdings": basket, "cost_prices": cost_p, "shares": shares, "capital": CAPITAL}
    _save_basket(data)

    # ── 5. 输出 ──────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Top 3 篮子  本金: ¥{CAPITAL:,}  {today}")
    print(f"{'='*60}")

    total_mv = 0; total_cost = 0
    for i, code in enumerate(basket, 1):
        name = nmap.get(code, "?")
        s    = score.get(code, 0)
        cp   = cost_p.get(code, 0)
        sh   = shares.get(code, 0)
        df = load_daily(code, (date.today()-timedelta(days=10)).strftime("%Y-%m-%d"), today)
        cur = float(df["close"].iloc[-1]) if not df.empty else cp
        mv = cur * sh
        pnl = (cur/cp - 1)*100 if cp > 0 else 0
        total_mv += mv; total_cost += cp * sh

        # MA10
        closes = df['close'].values
        ma10_val = closes[-10:].mean() if len(closes)>=10 else cur

        print(f"\n  {i}. {code} {name}")
        print(f"     得分:{s:.2f}  |  {sh}股 @{cp:.2f} → 现价{cur:.2f}")
        print(f"     市值: ¥{mv:,.0f}  盈亏:{pnl:+.1f}%  "
              f"MA10:{ma10_val:.2f} {'❌跌破' if cur<ma10_val else '✅'}")

    pnl_total = total_mv - total_cost
    print(f"\n  {'─'*60}")
    print(f"  总市值: ¥{total_mv:,.0f}  总成本: ¥{total_cost:,.0f}  "
          f"浮动盈亏: {pnl_total:+,.0f}")
    print(f"  今日操作: {'无，持有' if not to_remove else f'已替换{len(to_remove)}只'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
