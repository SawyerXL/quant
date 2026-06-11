"""
Track B 组合层：三层门控串联 → 选出最多6只持仓。

门控逻辑（AND，任一层不通过则无信号）：
  Layer1: Regime Gate → ATTACK/NEUTRAL/DEFENSE
  Layer2: 主线板块池（confirmed 状态）
  Layer3: 板块内 stock_score Top 2

风控：T+1、MA10连续3天止损、单票≤20%
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
from datetime import date
from loguru import logger
from config.strategy_params.trinity import PORTFOLIO, REGIME
from strategies.trinity.regime import RegimeGate
from strategies.trinity.sector import compute_sector_scores, update_sector_state
from strategies.trinity.stock_score import compute_stock_scores


class TrinityPortfolio:

    def __init__(self):
        self.gate = RegimeGate()
        self._prev_sector_state: dict | None = None

    # ------------------------------------------------------------------
    # 主接口：完整选股
    # ------------------------------------------------------------------
    def select(
        self,
        panel: pd.DataFrame,
        amount_panel: pd.DataFrame | None,
        stock_info: pd.DataFrame,
        trade_date: str,
        current_holdings: list[str] | None = None,
        days_below_ma10: dict[str, int] | None = None,
    ) -> dict:
        """
        返回信号字典，格式对齐 daily_signal_a.py：
          {signal_date, holdings, buy, sell, shares, prices, ...}
        """
        result = {
            "signal_date": trade_date, "holdings": [], "buy": [], "sell": [],
            "shares": {}, "prices": {}, "regime": "", "note": "",
        }

        # ── Layer 1: Regime ────────────────────────────
        regime = self.gate.evaluate(trade_date)
        result["regime"] = regime["state"]
        result["position_cap"] = regime["position_cap"]
        if regime["position_cap"] <= 0:
            result["note"] = f"DEFENSE 模式，清仓"
            if current_holdings:
                result["sell"] = list(current_holdings)
            return result

        # ── Layer 2: 板块 ──────────────────────────────
        sec_scores = compute_sector_scores(panel, amount_panel, stock_info, trade_date)
        if sec_scores.empty:
            result["note"] = "板块数据不足"
            return result
        sec_scores = update_sector_state(self._prev_sector_state, sec_scores, trade_date)
        self._prev_sector_state = sec_scores[["state", "days_in_state"]].to_dict("index")
        confirmed = sec_scores[sec_scores["state"] == "confirmed"]
        top_pool  = set(confirmed.head(PORTFOLIO["max_stocks"]).index)

        # ── Layer 3: 个股 ──────────────────────────────
        candidates = []
        from strategies.trinity.data_cache import get_sector_members
        smap = get_sector_members()
        for ind in confirmed.head(5).index:
            codes = smap.get(ind, [])
            stock_df = compute_stock_scores(panel, amount_panel, stock_info, codes, trade_date)
            if stock_df.empty:
                continue
            # 每板块取 Top 2
            top2 = stock_df.head(PORTFOLIO["max_per_sector"])
            for code, row in top2.iterrows():
                candidates.append((code, ind, row["score"]))

        candidates.sort(key=lambda x: x[2], reverse=True)
        # 最多6只
        selected = []
        seen_ind = {}
        for code, ind, sc in candidates:
            if len(selected) >= PORTFOLIO["max_stocks"]:
                break
            if seen_ind.get(ind, 0) >= PORTFOLIO["max_per_sector"]:
                continue
            selected.append(code)
            seen_ind[ind] = seen_ind.get(ind, 0) + 1

        # ── NEUTRAL: 不新开仓，只保留现有 ──────────────
        if regime["state"] == "NEUTRAL":
            if current_holdings:
                result["holdings"] = list(current_holdings)
                result["sell"] = [c for c in current_holdings if c not in selected]
                result["note"] = "NEUTRAL 模式，只减不增"
            else:
                result["note"] = "NEUTRAL 模式，禁止新开仓"
            return result

        # ── ATTACK: 正常选股 ───────────────────────────
        result["holdings"] = selected
        if current_holdings:
            cur_set = set(current_holdings)
            new_set = set(selected)
            result["sell"] = list(cur_set - new_set)
            result["buy"]  = list(new_set - cur_set)

        # 等权计算股数（简化：每只约 CAP/6 元）
        cap_per_stock = PORTFOLIO["capital"] / max(len(selected), 1)
        hist = panel[panel.index <= trade_date]
        cur_p = hist.iloc[-1] if len(hist) > 0 else pd.Series()
        for code in selected:
            price = float(cur_p.get(code, 0))
            if price > 0:
                shares = max(int(cap_per_stock / price / 100) * 100, 100)
                result["shares"][code] = shares
                result["prices"][code] = price
            else:
                result["shares"][code] = 0
                result["prices"][code] = 0

        # MA10 止损检查
        if days_below_ma10:
            ma10_exits = [c for c in selected if days_below_ma10.get(c, 0) >= PORTFOLIO["ma_exit_days"]]
            if ma10_exits:
                result["sell"] = list(set(result.get("sell", []) + ma10_exits))
                result["holdings"] = [c for c in result["holdings"] if c not in ma10_exits]
                result["ma10_exits"] = ma10_exits

        return result
