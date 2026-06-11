"""
Track B 组合层（v2 — 两层架构，无独立择时）

架构：
  Layer 1: 板块强度 → confirmed 板块池（top 5）
  Layer 2: 个股打分 → 每板块 top 2，总持仓≤6

风控（满仓运行，靠个股级风控）：
  MA10 连续3天止损、单票≤20%、板块退出标记后只减不增
  仓位 100%（不调仓时满仓），CSI2000 MA200调节已在审计中否定
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
from datetime import date
from loguru import logger
from config.strategy_params.trinity import PORTFOLIO, SECTOR
from strategies.trinity.sector import compute_sector_scores, update_sector_state
from strategies.trinity.stock_score import compute_stock_scores
from strategies.trinity.data_cache import get_sector_members


class TrinityPortfolio:

    def __init__(self):
        self._prev_sector_state: dict | None = None

    def warmup(self, panel: pd.DataFrame, amount_panel: pd.DataFrame | None,
               stock_info: pd.DataFrame, target_date: str):
        """
        从 T-120 个交易日回放状态机至 target_date。
        禁止手动设置状态———回测与实盘共用此路径。
        """
        from datetime import timedelta
        tgt = pd.Timestamp(target_date)
        lookback = tgt - pd.Timedelta(days=200)  # 取200自然日≈120交易日
        hist = panel[panel.index <= tgt]
        if len(hist) < 60:
            return  # 不足以回放，保持 None

        start_i = max(0, len(hist) - 120)
        for i in range(start_i, len(hist)):
            dt = hist.index[i].strftime("%Y-%m-%d")
            try:
                sec = compute_sector_scores(panel, amount_panel, stock_info, dt)
                if not sec.empty:
                    sec = update_sector_state(self._prev_sector_state, sec, dt)
                    self._prev_sector_state = sec[["state", "days_in_state"]].to_dict("index")
            except Exception:
                pass

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
        返回信号字典（对齐 daily_signal_a.py 格式）。
        """
        result = {
            "signal_date": trade_date, "holdings": [], "buy": [], "sell": [],
            "shares": {}, "prices": {}, "regime": "FULL", "note": "",
        }

        # ── Layer 1: 板块 ───────────────────────────────
        sec_scores = compute_sector_scores(panel, amount_panel, stock_info, trade_date)
        if sec_scores.empty:
            result["note"] = "板块数据不足"
            return result

        sec_scores = update_sector_state(self._prev_sector_state, sec_scores, trade_date)
        self._prev_sector_state = sec_scores[["state", "days_in_state"]].to_dict("index")
        confirmed = sec_scores[sec_scores["state"] == "confirmed"]

        if confirmed.empty:
            # 无确认主线：保留现有持仓，不新开
            result["holdings"] = current_holdings or []
            result["note"] = "无确认主线板块，持有现有仓位"
            return result

        # ── Layer 2: 个股 ───────────────────────────────
        smap = get_sector_members(SECTOR["level"])
        candidates = []
        top_pool = confirmed.head(SECTOR["top_n"])

        for ind in top_pool.index:
            codes = smap.get(ind, [])
            stock_df = compute_stock_scores(panel, amount_panel, stock_info, codes, trade_date)
            if stock_df.empty:
                continue
            top2 = stock_df.head(PORTFOLIO["max_per_sector"])
            for code, row in top2.iterrows():
                candidates.append((code, ind, row["score"]))

        candidates.sort(key=lambda x: x[2], reverse=True)
        selected = []
        seen_ind = {}
        max_n = PORTFOLIO["max_stocks"]
        max_ps = PORTFOLIO["max_per_sector"]

        for code, ind, sc in candidates:
            if len(selected) >= max_n:
                break
            if seen_ind.get(ind, 0) >= max_ps:
                continue
            # 板块退出标记：该板块持仓只减不增
            sector_st = sec_scores.loc[ind, "state"] if ind in sec_scores.index else "candidate"
            if sector_st == "exiting" and code not in (current_holdings or []):
                continue
            selected.append(code)
            seen_ind[ind] = seen_ind.get(ind, 0) + 1

        result["holdings"] = selected

        # 买卖差量
        if current_holdings:
            cur_set = set(current_holdings)
            new_set = set(selected)
            result["sell"] = list(cur_set - new_set)
            result["buy"]  = list(new_set - cur_set)

        # 仓位计算（单票≤20%，未分配部分持现金）
        cap = PORTFOLIO["capital"]
        max_per = cap * PORTFOLIO["max_single_pct"]
        equal   = cap / max(len(selected), 1)
        per = min(equal, max_per)
        hist = panel[panel.index <= trade_date]
        cur_p = hist.iloc[-1] if len(hist) > 0 else pd.Series()
        for code in selected:
            price = float(cur_p.get(code, 0))
            if price > 0:
                qty = max(int(per / price / 100) * 100, 100)
                result["shares"][code] = qty
                result["prices"][code] = price

        # MA10 止损
        if days_below_ma10:
            exits = [c for c in (current_holdings or [])
                     if days_below_ma10.get(c, 0) >= PORTFOLIO["ma_exit_days"]]
            if exits:
                result["sell"] = list(set(result.get("sell", []) + exits))
                result["holdings"] = [c for c in result["holdings"] if c not in exits]
                result["ma10_exits"] = exits

        return result
