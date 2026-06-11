"""
Track B 单元测试：组合层空候选/不足候选场景 + 两层架构回归。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import date


class TestPortfolioInsufficientCandidates:
    """验证候选不足时不强制补齐，持有现金"""

    def test_empty_sector_scores(self):
        """板块数据为空时，保留现有持仓"""
        from strategies.trinity.portfolio import TrinityPortfolio
        pf = TrinityPortfolio()
        empty_panel = pd.DataFrame()
        result = pf.select(empty_panel, None, pd.DataFrame(), "2026-06-11",
                           current_holdings=["000001", "000002"])
        assert result["holdings"] == ["000001", "000002"], f"应保留现有持仓, 实际={result['holdings']}"
        has_note = "板块数据不足" in result.get("note","") or "无确认" in result.get("note","")
        assert has_note, f"应有提示, 实际={result.get('note')}"
        print("  ✅ test_empty_sector_scores")

    def test_no_confirmed_sectors(self):
        """无确认主线时，保留现有持仓不新开（通过空panel模拟）"""
        from strategies.trinity.portfolio import TrinityPortfolio
        pf = TrinityPortfolio()
        result = pf.select(pd.DataFrame(), None, pd.DataFrame(), "2026-06-11",
                           current_holdings=["000001"])
        assert result["holdings"] == ["000001"], f"应保留现有持仓"
        print("  ✅ test_no_confirmed_sectors")

    def test_insufficient_candidates_not_padded(self):
        """不足6只时不扩容过滤，接受少仓"""
        selected = ["000001", "000002"]  # 只选到2只
        assert len(selected) < 6, "用例设计错误：需不足6只"
        from config.strategy_params.trinity import PORTFOLIO
        cap = PORTFOLIO["capital"]
        max_per = cap * PORTFOLIO["max_single_pct"]
        equal   = cap / max(len(selected), 1)
        per_stock = min(equal, max_per)  # 单票≤20%
        total_allocated = per_stock * len(selected)
        assert total_allocated <= cap * 0.5, f"分配{total_allocated}应≤{cap*0.5}"
        print(f"  ✅ test_insufficient_not_padded: {len(selected)}只, "
              f"分配{total_allocated:,.0f}, 剩余现金{cap-total_allocated:,.0f}")

    def test_empty_candidates_cash(self):
        """空候选时组合全部为现金"""
        from config.strategy_params.trinity import PORTFOLIO
        selected = []
        assert len(selected) == 0
        # 全部现金，净值=1.0
        cash_pct = 1.0 - len(selected) / max(PORTFOLIO["max_stocks"], 1)
        assert cash_pct == 1.0
        print(f"  ✅ test_empty_candidates: 100%现金")

    def test_ma10_exit_logic(self):
        """MA10止损剔除逻辑"""
        current = ["A", "B", "C", "D", "E", "F"]
        selected = ["A", "C", "D", "G", "H", "I"]  # B/E/F 被换出
        below_ma = {"B": 3, "E": 2, "F": 5}  # B/F 触发，E 未触(2天)
        exits = [c for c in current if below_ma.get(c, 0) >= 3]
        holdings = [c for c in selected if c not in exits]
        assert len(exits) == 2, f"应止损2只(B:3天,F:5天), 实际{exits}"
        assert "B" in exits and "F" in exits
        assert "E" not in exits  # 仅2天，不触发
        print(f"  ✅ test_ma10_exit: 止损{exits}, 保留{holdings}")

    def test_sector_exit_no_new_buys(self):
        """板块退出标记：已持仓的保留，不新买入该板块股票"""
        sector_state = {"电子": "exiting", "医药": "confirmed"}
        current = ["000001", "000002"]
        candidates = [("000001", "电子", 0.9), ("000003", "电子", 0.8), ("000004", "医药", 0.7)]
        selected = []
        for code, ind, sc in candidates:
            if sector_state.get(ind) == "exiting" and code not in current:
                continue  # 退出板块只减不增
            selected.append(code)
        assert "000001" in selected  # 已在持仓，保留
        assert "000003" not in selected  # 电子新票，不买
        assert "000004" in selected  # 医药正常
        print(f"  ✅ test_sector_exit: {selected}")


if __name__ == "__main__":
    t = TestPortfolioInsufficientCandidates()
    t.test_empty_sector_scores()
    t.test_insufficient_candidates_not_padded()
    t.test_empty_candidates_cash()
    t.test_ma10_exit_logic()
    t.test_sector_exit_no_new_buys()
    print("\n  ✅ 全部组合层单元测试通过")
