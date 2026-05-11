"""
RiskGateway 单元测试。

覆盖：
  - 账户回撤熔断
  - 单笔金额上限（防错单）
  - 单股持仓上限（Track A 5% / Track B 8%）
  - 现金不足拦截
  - 卖出方向豁免持仓检查
  - 已知 stub（涨跌停检查、行业集中度）文档化测试

运行：
    cd /root/quant && .venv/bin/python -m pytest tests/test_risk.py -v
"""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.risk import RiskGateway


# ── 测试用账户状态构造器 ────────────────────────────────────────────

def make_state(
    total_assets: float = 1_000_000,
    cash: float = 500_000,
    positions: dict = None,
    nav_high: float = 1.0,
    current_nav: float = 1.0,
    strategy_positions: dict = None,
):
    return {
        "total_assets": total_assets,
        "cash": cash,
        "positions": positions or {},
        "nav_high": nav_high,
        "current_nav": current_nav,
        "strategy_positions": strategy_positions or {"track_a": {}, "track_b": {}},
    }


# ══════════════════════════════════════════════════
# 1. 账户回撤熔断
# ══════════════════════════════════════════════════

class TestAccountDrawdown:

    def test_no_drawdown_passes(self):
        gw = RiskGateway(make_state(nav_high=1.0, current_nav=1.0))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert ok, msg

    def test_small_drawdown_passes(self):
        # 15% 回撤，未达 25% 熔断线
        gw = RiskGateway(make_state(nav_high=1.0, current_nav=0.85))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert ok, msg

    def test_exactly_at_limit_is_blocked(self):
        # 25% 回撤 = 熔断线，应拒绝
        gw = RiskGateway(make_state(nav_high=1.0, current_nav=0.75))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert not ok
        assert "熔断" in msg

    def test_beyond_limit_is_blocked(self):
        # 30% 回撤，超过 25% 熔断
        gw = RiskGateway(make_state(nav_high=1.0, current_nav=0.70))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert not ok

    def test_sell_also_blocked_during_drawdown(self):
        # 熔断期间卖出也应被拦截（保护机制）
        gw = RiskGateway(make_state(nav_high=1.0, current_nav=0.70))
        ok, msg = gw.check("track_a", "000001", "sell", 100, 10.0)
        assert not ok
        assert "熔断" in msg


# ══════════════════════════════════════════════════
# 2. 单笔金额防错单（上限 5万）
# ══════════════════════════════════════════════════

class TestOrderAmount:

    def _big_account(self, cash=500_000):
        """用千万大账户隔离金额检查，避免被持仓上限提前拦截。"""
        return make_state(total_assets=10_000_000, cash=cash)

    def test_within_limit_passes(self):
        # 100股 × 990 = 99,000 < 100,000（在大账户里持仓比例仅0.99%，不触发持仓上限）
        gw = RiskGateway(self._big_account())
        ok, msg = gw.check("track_a", "000001", "buy", 100, 990.0)
        assert ok, msg

    def test_exactly_at_limit_passes(self):
        # 100 × 1,000 = 100,000 = 上限，应通过
        gw = RiskGateway(self._big_account(cash=200_000))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 1000.0)
        assert ok, msg

    def test_over_limit_is_blocked(self):
        # 100 × 1,001 = 100,100 > 100,000（防错单）
        gw = RiskGateway(self._big_account())
        ok, msg = gw.check("track_a", "000001", "buy", 100, 1001.0)
        assert not ok
        assert "上限" in msg or "超过" in msg

    def test_large_order_blocked(self):
        # 典型错单：多打一个0 → 1000股 × 500 = 500,000
        gw = RiskGateway(self._big_account())
        ok, msg = gw.check("track_a", "000001", "buy", 1000, 500.0)
        assert not ok

    def test_sell_large_order_also_blocked(self):
        # 卖出大单同样受金额限制（防错单保护双向）
        gw = RiskGateway(self._big_account())
        ok, msg = gw.check("track_a", "000001", "sell", 1000, 500.0)
        assert not ok


# ══════════════════════════════════════════════════
# 3. 单股持仓上限
# ══════════════════════════════════════════════════

class TestSinglePosition:

    def test_track_a_within_5pct_passes(self):
        # 总资产100万，买入后该股=4.9万 < 5%
        gw = RiskGateway(make_state(total_assets=1_000_000))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 490.0)
        assert ok, msg

    def test_track_a_exceeds_5pct_is_blocked(self):
        # 总资产100万，买入后该股=5.1万 > 5%
        gw = RiskGateway(make_state(total_assets=1_000_000))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 510.0)
        assert not ok
        assert "上限" in msg or "超过" in msg

    def test_track_a_existing_position_cumulative(self):
        # 已有 3万该股持仓，再买 2.5万 → 共 5.5万 > 5%
        state = make_state(
            total_assets=1_000_000,
            positions={"000001": 30_000},
        )
        gw = RiskGateway(state)
        ok, msg = gw.check("track_a", "000001", "buy", 100, 250.0)
        assert not ok

    def test_track_b_allows_up_to_8pct(self):
        # Track B 上限8%（总资产100万）：买入7.9万 / 100万 = 7.9% < 8%
        # 注：MAX_SINGLE_ORDER_VALUE 已调整为10万，此处测试位置上限而非金额上限
        gw = RiskGateway(make_state(total_assets=1_000_000, cash=200_000))
        ok, msg = gw.check("track_b", "000001", "buy", 100, 790.0)
        assert ok, msg

    def test_track_b_exceeds_8pct_is_blocked(self):
        # 8.1万 / 100万 = 8.1% > 8%，应被持仓上限拦截
        gw = RiskGateway(make_state(total_assets=1_000_000, cash=200_000))
        ok, msg = gw.check("track_b", "000001", "buy", 100, 810.0)
        assert not ok

    def test_sell_bypasses_position_check(self):
        # 卖出不检查持仓上限（无论持仓多少都允许卖）
        state = make_state(
            total_assets=1_000_000,
            positions={"000001": 100_000},   # 已有10%，超上限
        )
        gw = RiskGateway(state)
        # 卖出时不应被单股持仓检查拦截
        ok, msg = gw.check("track_a", "000001", "sell", 100, 10.0)
        # 只检查熔断和金额，不检查持仓
        assert ok, f"卖出不应被持仓检查拦截: {msg}"


# ══════════════════════════════════════════════════
# 4. 现金不足
# ══════════════════════════════════════════════════

class TestCashAvailable:

    def test_sufficient_cash_passes(self):
        gw = RiskGateway(make_state(cash=50_000))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 490.0)
        assert ok, msg

    def test_insufficient_cash_blocked(self):
        # 现金只有2万，要买4.9万
        gw = RiskGateway(make_state(cash=20_000))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 490.0)
        assert not ok
        assert "现金" in msg or "不足" in msg

    def test_sell_bypasses_cash_check(self):
        # 卖出不需要现金
        gw = RiskGateway(make_state(cash=0))
        ok, msg = gw.check("track_a", "000001", "sell", 100, 10.0)
        assert ok, f"卖出不应被现金检查拦截: {msg}"

    def test_cash_includes_commission_buffer(self):
        # 买入金额 = 10,000，含手续费需 10,030；现金 10,020 应被拒绝
        gw = RiskGateway(make_state(cash=10_020))
        ok, msg = gw.check("track_a", "000001", "buy", 100, 100.0)
        assert not ok, "需含手续费余量"


# ══════════════════════════════════════════════════
# 5. 综合场景
# ══════════════════════════════════════════════════

class TestCombinedScenarios:

    def test_typical_track_a_buy_passes(self):
        """Track A 正常买入：2万元仓位，账户余额充足"""
        state = make_state(
            total_assets=1_000_000,
            cash=600_000,
            nav_high=1.0,
            current_nav=1.05,
        )
        gw = RiskGateway(state)
        ok, msg = gw.check("track_a", "000001", "buy", 100, 195.0)  # 19,500 < 20,000
        assert ok, msg

    def test_drawdown_blocks_all_strategies(self):
        """熔断后两个策略都应被拦截"""
        state = make_state(nav_high=1.0, current_nav=0.70)
        gw = RiskGateway(state)
        ok_a, _ = gw.check("track_a", "000001", "buy", 100, 10.0)
        ok_b, _ = gw.check("track_b", "000002", "buy", 100, 10.0)
        assert not ok_a
        assert not ok_b

    def test_multiple_rejection_returns_first_failure(self):
        """多项同时不满足时，返回第一个失败原因（账户熔断优先）"""
        state = make_state(
            total_assets=1_000_000,
            cash=0,              # 现金不足
            nav_high=1.0,
            current_nav=0.70,   # 熔断
        )
        gw = RiskGateway(state)
        ok, msg = gw.check("track_a", "000001", "buy", 100, 10.0)
        assert not ok
        assert "熔断" in msg   # 熔断检查排第一位


# ══════════════════════════════════════════════════
# 6. 已知 Stub / 待完善功能（文档化测试）
# ══════════════════════════════════════════════════

class TestKnownStubs:

    def test_limit_price_check_is_stub(self):
        """
        [已知] _check_limit_price 是 stub，始终返回 True。
        接入 QMT 实时行情后需实现：涨停不买 / 跌停不卖。
        此测试确认当前行为，并作为接入后的回归基准。
        """
        gw = RiskGateway(make_state())
        # 即使模拟涨停价（极高价格），当前实现也会放行
        ok, _ = gw.check("track_a", "000001", "buy", 100, 999999.0)
        # 注：此处因金额超限会被 _check_order_amount 拦截，
        # 单独测 _check_limit_price 需绕过其他检查
        result = gw._check_limit_price("track_a", "000001", "buy", 100, 100.0, 10000, {})
        assert result == (True, ""), "涨跌停检查是 stub，接入 QMT 后需更新此测试"

    def test_industry_concentration_not_called(self):
        """
        [已知缺陷] _check_industry_concentration 定义了但未加入 checks 列表。
        行业集中度无法被检查。需修复或在此测试通过后删除此 TODO。
        """
        state = make_state(
            total_assets=1_000_000,
            cash=600_000,
            strategy_positions={
                "track_a": {f"00000{i}": 40_000 for i in range(8)},  # 已持有8只同行业股
                "track_b": {},
            }
        )
        gw = RiskGateway(state)
        # 当前代码行业集中度未被检查，此买入会被放行（不应该）
        ok, _ = gw.check("track_a", "999999", "buy", 100, 200.0)
        # 此 assert 记录当前行为（放行），修复后应改为 assert not ok
        assert ok, "行业集中度检查未实现 — 已知缺陷，后续需修复"
