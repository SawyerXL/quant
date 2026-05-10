"""
交易执行器：把策略信号转成订单，经风控网关后送到 QMT 执行。
"""
import math
import pandas as pd
from loguru import logger

from config.settings import MIN_LOT
from execution.risk import RiskGateway
from execution.qmt_client import get_client
from monitoring.alerts import send_alert


class Trader:

    def __init__(self):
        self.client = get_client()

    def rebalance(
        self,
        strategy_id: str,
        target_weights: dict[str, float],  # {code: 目标权重}
        current_price: dict[str, float],   # {code: 当前价格}
    ) -> dict:
        """
        根据目标权重执行调仓。
        1. 读取当前持仓
        2. 计算差异（需要买入/卖出的量）
        3. 经风控校验
        4. 先卖后买
        5. 推送执行报告
        """
        account = self.client.get_account_info()
        positions = self.client.get_positions()
        total = account["total_assets"]

        gateway = RiskGateway({
            "total_assets": total,
            "cash":         account["cash"],
            "positions":    {c: p["market_value"] for c, p in positions.items()},
            "nav_high":     total,      # 简化处理，实际需持久化历史最高值
            "current_nav":  total,
            "strategy_positions": {strategy_id: {c: p["market_value"] for c, p in positions.items()}},
        })

        # 计算目标持仓量
        target_shares = {}
        for code, w in target_weights.items():
            price = current_price.get(code)
            if not price:
                continue
            target_value  = total * w
            raw_shares    = target_value / price
            target_shares[code] = int(raw_shares // MIN_LOT) * MIN_LOT

        # 计算差值
        all_codes = set(target_shares) | set(positions)
        sells, buys = [], []
        for code in all_codes:
            current = positions.get(code, {}).get("volume", 0)
            target  = target_shares.get(code, 0)
            diff    = target - current
            price   = current_price.get(code, 0)
            if diff < 0 and price > 0:
                sells.append((code, abs(diff), price))
            elif diff > 0 and price > 0:
                buys.append((code, diff, price))

        results = {"sells": [], "buys": [], "blocked": []}

        # 先卖出（回收现金）
        for code, shares, price in sells:
            ok, reason = gateway.check(strategy_id, code, "sell", shares, price)
            if ok:
                oid = self.client.place_order(code, "sell", shares, price * 0.998)
                results["sells"].append({"code": code, "shares": shares, "price": price, "order_id": oid})
            else:
                results["blocked"].append({"code": code, "direction": "sell", "reason": reason})

        # 后买入
        for code, shares, price in buys:
            ok, reason = gateway.check(strategy_id, code, "buy", shares, price)
            if ok:
                oid = self.client.place_order(code, "buy", shares, price * 1.002)
                results["buys"].append({"code": code, "shares": shares, "price": price, "order_id": oid})
            else:
                results["blocked"].append({"code": code, "direction": "buy", "reason": reason})

        # 推送执行报告
        msg = (
            f"[{strategy_id}] 调仓完成\n"
            f"卖出 {len(results['sells'])} 笔 / 买入 {len(results['buys'])} 笔 / 拦截 {len(results['blocked'])} 笔"
        )
        send_alert(msg)
        logger.info(msg)
        return results
