"""
风控网关：所有订单必须经过此模块校验，校验不通过直接拦截。
两套策略共用，通过 strategy_id 区分参数。
"""
from loguru import logger
from config.settings import MAX_SINGLE_ORDER_VALUE, MAX_ACCOUNT_DRAWDOWN
from config.strategy_params.multi_factor import STRATEGY_A
from config.strategy_params.trinity import STRATEGY_B


_STRATEGY_RISK = {
    "track_a": STRATEGY_A["risk"],
    "track_b": STRATEGY_B["risk"],
}


class RiskGateway:

    def __init__(self, account_state: dict):
        """
        account_state: {
            'total_assets': float,           # 账户总资产
            'cash': float,                   # 可用现金
            'positions': {code: value},      # 当前持仓（市值，全策略合计）
            'nav_high': float,               # 历史最高净值
            'current_nav': float,            # 当前净值
            'strategy_positions': {          # 各策略持仓市值
                'track_a': {code: value},
                'track_b': {code: value},
            },
            'industry_map': {code: str},     # 股票→申万一级行业，用于集中度检查
                                             # 缺省时跳过行业检查
        }
        """
        self.state = account_state

    def check(
        self,
        strategy_id: str,
        code: str,
        direction: str,   # 'buy' | 'sell'
        shares: int,
        price: float,
    ) -> tuple[bool, str]:
        """
        执行全部风控检查，返回 (通过, 原因)。
        任何一项不通过立即拒绝。
        """
        order_value = shares * price
        risk = _STRATEGY_RISK.get(strategy_id, _STRATEGY_RISK["track_a"])

        checks = [
            self._check_account_drawdown,
            self._check_order_amount,
            self._check_limit_price,
            self._check_single_position,
        ]
        if direction == "buy":
            checks += [
                self._check_cash_available,
                self._check_industry_concentration,
            ]

        for check_fn in checks:
            ok, reason = check_fn(
                strategy_id, code, direction, shares, price, order_value, risk
            )
            if not ok:
                logger.warning(f"[风控拦截] {strategy_id} {direction} {code} {shares}股: {reason}")
                return False, reason

        return True, "通过"

    # ------------------------------------------------------------------
    # 各检查项
    # ------------------------------------------------------------------
    def _check_account_drawdown(self, strategy_id, code, direction, shares, price, order_value, risk):
        drawdown = 1 - self.state["current_nav"] / self.state["nav_high"]
        if drawdown >= MAX_ACCOUNT_DRAWDOWN:
            return False, f"账户回撤 {drawdown:.1%} 已触及熔断线 {MAX_ACCOUNT_DRAWDOWN:.1%}"
        return True, ""

    def _check_order_amount(self, strategy_id, code, direction, shares, price, order_value, risk):
        if order_value > MAX_SINGLE_ORDER_VALUE:
            return False, f"单笔金额 {order_value:,.0f} 超过上限 {MAX_SINGLE_ORDER_VALUE:,}"
        return True, ""

    def _check_limit_price(self, strategy_id, code, direction, shares, price, order_value, risk):
        # 涨停不买、跌停不卖（调用前应传入涨跌停状态，此处用价格变动代理）
        # 实际接入 QMT 后用实时行情判断
        return True, ""

    def _check_single_position(self, strategy_id, code, direction, shares, price, order_value, risk):
        if direction != "buy":
            return True, ""
        max_pct = risk["max_single_position"]
        total   = self.state["total_assets"]
        current = self.state["positions"].get(code, 0)
        after   = current + order_value
        if after / total > max_pct:
            return False, (
                f"单股持仓 {after/total:.1%} 超过上限 {max_pct:.1%} "
                f"(当前={current:,.0f}, 拟买={order_value:,.0f})"
            )
        return True, ""

    def _check_cash_available(self, strategy_id, code, direction, shares, price, order_value, risk):
        if self.state["cash"] < order_value * 1.003:  # 留3‰手续费余量
            return False, f"可用现金 {self.state['cash']:,.0f} 不足 {order_value:,.0f}"
        return True, ""

    def _check_industry_concentration(self, strategy_id, code, direction, shares, price, order_value, risk):
        if direction != "buy":
            return True, ""

        industry_map = self.state.get("industry_map", {})
        target = industry_map.get(code)
        if not target or target == "其他":
            # 无行业信息或北交所等无法归类股票，宽松放行
            return True, ""

        # Track A 用 max_industry_position(30%)，Track B 用 max_sector_position(50%)
        max_pct = risk.get("max_industry_position") or risk.get("max_sector_position", 0.50)
        total   = self.state["total_assets"]

        # 当前策略同行业持仓市值汇总
        strat_pos = self.state["strategy_positions"].get(strategy_id, {})
        industry_exposure = sum(
            v for c, v in strat_pos.items()
            if industry_map.get(c) == target
        )

        after = industry_exposure + order_value
        if after / total > max_pct:
            return False, (
                f"行业集中度 [{target}] {after/total:.1%} 超过上限 {max_pct:.1%} "
                f"(当前={industry_exposure:,.0f}, 拟买={order_value:,.0f})"
            )
        return True, ""
