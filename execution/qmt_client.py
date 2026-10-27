"""
QMT 交易客户端封装。
此文件需要部署在 Windows 交易服务器上，依赖 xtquant（QMT 附带）。
本地 Mac 开发时导入会失败，用 MockQMTClient 替代。
"""
from loguru import logger
from pathlib import Path
from config.settings import QMT_PATH, QMT_ACCOUNT_ID, IS_PROD

# 链隔离锁路径(2026-10-09 用户裁定): 人工档3执行会话开始时置锁,
# 结束时解锁; place_order 见锁即拒绝全部自动委托。共享库目录使
# Linux/Windows 同一路径语义(H:/quant 与 /root/quant 各自读各自锁,
# 故锁文件由人工会话在**执行侧**(Windows)与本地同步设置——人工执行
# 时无自动任务并行, Windows 侧锁为真拦截点, Linux 侧锁为双保险)。
MANUAL_EXEC_LOCK = "logs/manual_exec_lock.json"

try:
    # 注意：不导入 xtdata（行情模块，需要QMT行情服务，会导致ImportError）
    # 只导入交易相关模块
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    import xtquant.xtconstant as xtc
    QMT_AVAILABLE = True
except ImportError:
    QMT_AVAILABLE = False
    logger.warning("xtquant 未安装（非 Windows/未装 QMT），使用 MockQMTClient")


def _to_xt_code(code: str) -> str:
    """将6位股票代码转为 xtquant 格式（加交易所后缀）。

    2026-09-09 normalize 修复: 委托给 data.code_norm.normalize_code
    （后缀规则唯一出处，禁止各模块自行拼接）; 行为差异=43/83/87 旧
    北交所号段从此 .BJ（此前 .SZ 误判, 实盘池不含此类码, 无实际影响）。
    """
    from data.code_norm import normalize_code
    return normalize_code(code)


def _tick_price(code: str, price: float) -> float:
    """委托价归一为价位(tick)整数倍——所有下单路径的单一收口。

    2026-09-03 教训固化: 价格乘数(×1.05/×0.998)产生5.89818这类多位小数,
    柜台按'委托价不符合价位设定'废单(9/3 32笔、8/14 8笔历史废单同根因)。
    股票/沪转债 tick=0.01; 深转债(12xxxx/127/128)/基金 tick=0.001。
    """
    code = str(code).zfill(6)
    if code.startswith(("12", "127", "128", "5", "15", "16", "18", "51")):
        tick = 0.001
    else:
        tick = 0.01
    # 末尾round(3)清掉浮点残渣(如199.70000000000002会再触发价位校验)
    return round(round(price / tick) * tick, 3)


class QMTClient:
    """生产环境 QMT 客户端（仅限 Windows 服务器）。"""

    def __init__(self):
        if not QMT_AVAILABLE:
            raise RuntimeError("xtquant 不可用，请在 Windows QMT 环境中运行")
        import time
        # 用时间戳作为 session_id，避免复用旧会话导致返回缓存废单
        session_id = int(time.time()) % 100000
        self.trader = XtQuantTrader(QMT_PATH, session_id)
        self.account = StockAccount(QMT_ACCOUNT_ID)
        self.trader.start()
        time.sleep(2)        # 等待服务器就绪
        result = self.trader.connect()
        if result != 0:
            raise RuntimeError(f"QMT 连接失败(result={result})，请确认 Matrix 终端已登录")
        time.sleep(1)
        self.trader.subscribe(self.account)   # 订阅账户后才能下单
        time.sleep(1)
        logger.info(f"QMT 连接成功: account={QMT_ACCOUNT_ID}")

    def get_positions(self) -> dict:
        """返回 {code: {'volume': int, 'cost_price': float, 'market_value': float}}"""
        positions = self.trader.query_stock_positions(self.account)
        return {
            p.stock_code: {
                "volume":       p.volume,
                "cost_price":   p.open_price,
                "market_value": p.market_value,
            }
            for p in (positions or [])
        }

    def get_account_info(self) -> dict:
        """返回 {total_assets, cash, market_value}"""
        info = self.trader.query_stock_asset(self.account)
        return {
            "total_assets": info.total_asset,
            "cash":         info.cash,
            "market_value": info.market_value,
        }

    def place_order(
        self,
        code: str,
        direction: str,   # 'buy' | 'sell'
        shares: int,
        price: float,
        order_type: str = "limit",
    ) -> int:
        """
        下单，返回 order_id（-1 表示失败）。
        price=-1 且 order_type='market' 表示市价单（A股限用于特殊场景）。
        """
        if shares <= 0:
            return -1

        # 2026-10-09 链隔离锁(用户裁定): 人工执行时段锁文件在场 → 拒绝所有
        # 自动委托(F2 考试日 127027 档1自动卖出事故的机制修复)。锁含日期+
        # 会话ID; 人工会话开始时置锁、结束时解锁; 拒绝是可见事件记日志。
        # 2026-10-10 补: 锁过期(日期≠当日)仍拒绝但发告警——忘解锁会在无提示
        # 下停掉全部交易(含临期强制卖出), 与"没人看得到的状态"同族。
        lock = Path(MANUAL_EXEC_LOCK)
        if lock.exists():
            stale = False
            try:
                import json as _j
                from datetime import date as _d
                _info = _j.loads(lock.read_text(encoding="utf-8"))
                if str(_info.get("date", "")) != str(_d.today()):
                    stale = True
            except Exception:
                pass
            logger.error(f"[链隔离锁] 拒绝下单 {direction} {code} {shares}股: "
                         f"人工执行锁在场({lock.name})"
                         f"{'⚠锁日期非当日=疑似忘解锁' if stale else ''}")
            if stale:
                try:
                    from monitoring.alerts import send_alert
                    send_alert("[链隔离锁] 锁日期非当日, 自动链被锁且疑似忘解锁"
                               "——请确认人工执行是否仍在进行, 结束请移除锁文件",
                               level="warning")
                except Exception:
                    pass
            return -1

        xt_direction = xtc.STOCK_BUY if direction == "buy" else xtc.STOCK_SELL
        if order_type == "market":
            xt_price_type = getattr(xtc, "LATEST_PRICE", 5)
        elif order_type == "best":
            xt_price_type = getattr(xtc, "MARKET_BEST", 5)
        else:
            xt_price_type = xtc.FIX_PRICE

        # tick归一: 防止'委托价不符合价位设定'废单(2026-09-03教训固化)
        if price > 0:
            price = _tick_price(code, price)

        order_id = self.trader.order_stock(
            self.account,
            _to_xt_code(code),
            xt_direction,
            shares,
            xt_price_type,
            price,
            strategy_name="quant",
            order_remark=f"{direction}_{shares}",
        )
        logger.info(f"下单: {direction} {code} {shares}股 @{price} → order_id={order_id}")
        return order_id

    def cancel_order(self, order_id: int) -> bool:
        ret = self.trader.cancel_order_stock(self.account, order_id)
        logger.info(f"撤单: order_id={order_id}, result={ret}")
        return ret == 0

    def get_today_orders(self) -> list[dict]:
        orders = self.trader.query_stock_orders(self.account, cancelable_only=False)
        return [
            {
                "order_id":    o.order_id,
                "code":        o.stock_code,
                "direction":   "buy" if o.order_type == xtc.STOCK_BUY else "sell",
                "shares":      o.order_volume,
                "price":       o.price,
                "traded_price": o.traded_price,   # 2026-09-24: 成交回报价(滑点真值)
                "filled":      o.traded_volume,
                "status":      o.order_status,
            }
            for o in (orders or [])
        ]

    def disconnect(self):
        self.trader.stop()
        logger.info("QMT 断开连接")


class MockQMTClient:
    """
    本地开发 / 模拟盘用的 Mock 客户端。
    行为与 QMTClient 完全一致，但不实际下单。
    """

    def __init__(self):
        self._positions = {}
        self._cash = 1_000_000.0
        self._orders = []
        logger.info("MockQMTClient 已启动（模拟模式）")

    def get_positions(self) -> dict:
        return self._positions.copy()

    def get_account_info(self) -> dict:
        mv = sum(p["market_value"] for p in self._positions.values())
        return {
            "total_assets": self._cash + mv,
            "cash":         self._cash,
            "market_value": mv,
        }

    def place_order(self, code, direction, shares, price, order_type="limit") -> int:
        if Path(MANUAL_EXEC_LOCK).exists():
            logger.error(f"[链隔离锁] Mock 拒绝下单 {direction} {code} {shares}股: "
                         f"人工执行锁在场")
            return -1
        order_id = len(self._orders) + 1
        value = shares * price
        if direction == "buy":
            self._cash -= value * 1.00025
            self._positions[code] = {
                "volume":       shares,
                "cost_price":   price,
                "market_value": value,
            }
        else:
            self._cash += value * (1 - 0.00125)
            self._positions.pop(code, None)
        self._orders.append({"order_id": order_id, "code": code, "direction": direction,
                              "shares": shares, "price": price, "filled": shares, "status": "filled"})
        logger.info(f"[Mock] {direction} {code} {shares}股 @{price:.2f}")
        return order_id

    def cancel_order(self, order_id): return True
    def get_today_orders(self): return self._orders.copy()
    def disconnect(self): pass


def get_client():
    """工厂函数：simulation/production 必须真实 QMT；仅 development 允许 Mock。

    2026-10-27 修复: 原逻辑在 ENV=simulation/production 但 xtquant 不可用时
    静默回退 Mock，日志会显示“成交成功”但实际没有委托（与 fillna(0) 同型）。
    仿真/生产环境 QMT 不可用必须 fail-fast，不能把 Mock 当真实账户。
    """
    import os as _os
    _env = _os.getenv("ENV", "development").lower()
    if _env in ("simulation", "production"):
        if not QMT_AVAILABLE:
            raise RuntimeError(
                f"ENV={_env} 但 xtquant 不可用；拒绝回退 MockQMTClient"
                "（请检查 Windows/QMT 环境与 xtquant 安装）")
        return QMTClient()
    return MockQMTClient()
