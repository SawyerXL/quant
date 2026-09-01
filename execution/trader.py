"""
交易执行器：把策略信号转成订单，经风控网关后送到 QMT 执行。
支持两种调用方式：
  1. execute_signal(json_path)   ← 读取信号文件，直接执行（生产用）
  2. rebalance(target_weights)   ← 按目标权重调仓（策略层调用）
"""
import json
from pathlib import Path

from loguru import logger

from config.settings import MIN_LOT
from data.storage import load_meta
from execution.risk import RiskGateway
from execution.qmt_client import get_client
from monitoring.alerts import send_alert

_NAV_HIGH_FILE = Path("data_store/meta/nav_high.json")


def _load_nav_high(strategy_id: str) -> float:
    """读取历史最高净值（用于熔断检查）。"""
    if _NAV_HIGH_FILE.exists():
        data = json.loads(_NAV_HIGH_FILE.read_text(encoding="utf-8"))
        return float(data.get(strategy_id, 1.0))
    return 1.0


def _save_nav_high(strategy_id: str, value: float):
    """持久化历史最高净值。"""
    _NAV_HIGH_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if _NAV_HIGH_FILE.exists():
        data = json.loads(_NAV_HIGH_FILE.read_text(encoding="utf-8"))
    data[strategy_id] = max(value, data.get(strategy_id, 0.0))
    _NAV_HIGH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _build_industry_map() -> dict[str, str]:
    """从 stock_info_full 构建行业映射，用于风控集中度检查。"""
    info = load_meta("stock_info_full")
    if info.empty or "industry_l1" not in info.columns:
        return {}
    return dict(zip(info["code"], info["industry_l1"]))


class Trader:

    def __init__(self):
        self.client = get_client()

    # ── 主要接口：读信号文件执行 ─────────────────────────────────────

    def execute_signal(self, signal_file: str | Path, strategy_id: str) -> dict:
        """
        读取信号 JSON，执行调仓。
        信号文件格式（由 daily_signal_a/b.py 生成）：
          {
            "regime": "bull" | "bear",
            "holdings": [...],
            "buy": [...],
            "sell": [...],
            "shares": {code: int},     # 目标手数（已计算好）
            "prices": {code: float},   # 信号日收盘价（作为参考价）
          }
        """
        path = Path(signal_file)
        if not path.exists():
            logger.error(f"信号文件不存在: {path}")
            return {"error": "信号文件不存在"}

        sig = json.loads(path.read_text(encoding="utf-8"))
        logger.info(f"[{strategy_id}] 读取信号: {sig.get('signal_date')} "
                    f"regime={sig.get('regime')} holdings={len(sig.get('holdings', []))}")

        # 熊市信号：清仓
        if sig.get("regime") == "bear":
            return self._execute_bear(strategy_id, sig)

        # 牛市信号：按 shares 字段直接执行差量调仓
        return self._execute_bull(strategy_id, sig)

    # ── 内部执行逻辑 ─────────────────────────────────────────────────

    def _build_gateway(self, strategy_id: str) -> tuple[RiskGateway, dict]:
        """构建风控网关，同时返回账户信息。"""
        account   = self.client.get_account_info()
        positions = self.client.get_positions()
        total     = account["total_assets"]
        nav_high  = max(_load_nav_high(strategy_id), total)
        _save_nav_high(strategy_id, total)

        gw = RiskGateway({
            "total_assets":   total,
            "cash":           account["cash"],
            "positions":      {c: p["market_value"] for c, p in positions.items()},
            "nav_high":       nav_high,
            "current_nav":    total,
            "strategy_positions": {
                strategy_id: {c: p["market_value"] for c, p in positions.items()}
            },
            "industry_map":   _build_industry_map(),
        })
        return gw, account

    def _execute_bear(self, strategy_id: str, sig: dict) -> dict:
        """熊市：卖出本track所有持仓（CB清仓只卖转债，股票清仓只卖股票）。"""
        gw, _ = self._build_gateway(strategy_id)
        positions = {c: v for c, v in self.client.get_positions().items()
                     if self._in_scope(c, strategy_id)}
        results = {"sells": [], "buys": [], "blocked": []}

        for code, pos in positions.items():
            shares = pos.get("volume", 0)
            price  = pos.get("cost_price", 1.0)
            if shares <= 0:
                continue
            ok, reason = gw.check(strategy_id, code, "sell", shares, price)
            if ok:
                oid = self.client.place_order(code, "sell", shares, price * 0.998)
                results["sells"].append({"code": code, "shares": shares, "order_id": oid})
            else:
                results["blocked"].append({"code": code, "direction": "sell", "reason": reason})

        msg = (f"[{strategy_id}] 熊市清仓: "
               f"卖出{len(results['sells'])}笔 / 拦截{len(results['blocked'])}笔")
        logger.warning(msg)
        send_alert(msg, level="warning")
        return results

    @staticmethod
    def _in_scope(code: str, strategy_id: str) -> bool:
        """按track隔离持仓域: CB策略只碰转债(11/12/127前缀), 股票策略只碰股票。
        2026-09-01 事故: CB执行器把账户股票当成'CB该清掉的持仓'误卖(600276)。"""
        cb = str(code).split(".")[0][:3] in ("110", "111", "113", "118", "123", "127", "128")
        if "cb" in strategy_id:
            return cb
        return not cb

    def _execute_bull(self, strategy_id: str, sig: dict) -> dict:
        """目标仓位 diff 式下单（2026-08-31 幂等化改造）。

        只下"目标持仓 − 实际持仓"的差量：重复执行/重试会收敛到同一状态，
        杜绝增量式指令重发导致的重复下单与仓位漂移。先卖后买。
        """
        gw, account = self._build_gateway(strategy_id)
        # 统一归一化: 信号code无后缀('600276'), positions/orders带后缀('600276.SH')
        # 2026-09-01 两次事故根因: key格式错位导致diff把持仓当0 → 重复下单
        def _norm(c):
            return str(c).split(".")[0]
        positions = {_norm(k): v for k, v in self.client.get_positions().items()
                     if self._in_scope(k, strategy_id)}
        # 成交回报延迟修正: 当日委托净额并入持仓口径(已下未回报的买单不再被当缺失)
        try:
            for o in self.client.get_today_orders():
                code = _norm(o.get("code"))
                if not self._in_scope(code, strategy_id):
                    continue
                d = o.get("direction"); q = o.get("shares", 0)
                cur = positions.setdefault(code, {"volume": 0, "market_value": 0})
                if d == "buy":
                    cur["volume"] += q
                elif d == "sell":
                    cur["volume"] -= q
        except Exception:
            pass
        target_shares = {_norm(k): v for k, v in sig.get("shares", {}).items()}
        holdings      = {_norm(c) for c in sig.get("holdings", [])}
        sell_set      = {_norm(c) for c in sig.get("sell", [])}
        ref_prices    = {_norm(k): v for k, v in sig.get("prices", {}).items()}
        results = {"sells": [], "buys": [], "blocked": []}

        # ── 卖出差量：清仓票全卖 / 目标<实际 补卖差量 ──
        for code in sorted(set(positions) | set(target_shares)):
            cur = positions.get(code, {}).get("volume", 0)
            if code in sell_set or (cur > 0 and code not in holdings):
                sell_qty = cur                      # 清仓
            elif cur > target_shares.get(code, 0):
                sell_qty = cur - target_shares.get(code, 0)   # 减仓差量
            else:
                sell_qty = 0
            if sell_qty <= 0:
                continue
            price = ref_prices.get(code) or positions.get(code, {}).get("cost_price", 1.0)
            # 卖出分笔: 单笔不超过防错单红线(10万), 超配收敛14万/笔会被拦
            MAX_ORD = 100_000
            remaining = sell_qty
            while remaining > 0:
                q = min(remaining, int(MAX_ORD / max(price, 0.01)))
                if q <= 0:
                    q = remaining
                ok, reason = gw.check(strategy_id, code, "sell", q, price)
                if not ok:
                    results["blocked"].append({"code": code, "direction": "sell", "reason": reason})
                    break
                oid = self.client.place_order(code, "sell", q, price * 0.998)
                results["sells"].append({"code": code, "shares": q,
                                         "price": price, "order_id": oid})
                logger.info(f"卖出 {code} {q}股 @{price:.2f}")
                remaining -= q

        # ── 买入差量：目标 − 实际，只买缺的部分 ──
        for code, tgt in target_shares.items():
            if code in sell_set:
                continue
            cur = positions.get(code, {}).get("volume", 0)
            buy_qty = tgt - cur
            price  = ref_prices.get(code, 0)
            if buy_qty <= 0 or price <= 0:
                continue
            ok, reason = gw.check(strategy_id, code, "buy", buy_qty, price)
            if ok:
                # 买入用 1.05 倍报价（A股限价单，超过市价时以市价成交）
                # 避免信号价格过期导致挂单不成交
                oid = self.client.place_order(code, "buy", buy_qty, price * 1.05)
                results["buys"].append({"code": code, "shares": buy_qty,
                                        "price": price, "order_id": oid})
                logger.info(f"买入 {code} {buy_qty}股 @{price:.2f}")
            else:
                results["blocked"].append({"code": code, "direction": "buy", "reason": reason})

        # ── 幂等对账：执行后复查，未收敛只告警不自动重试（防重复下单） ──
        try:
            actual = self.client.get_positions()
            drift = {}
            for c, t in target_shares.items():
                av = actual.get(c, {}).get("volume", 0)
                if abs(av - t) >= 100:
                    drift[c] = (av, t)
            for c in sell_set:
                if actual.get(c, {}).get("volume", 0) >= 100:
                    drift[c] = (actual[c]["volume"], 0)
            if drift:
                dmsg = "; ".join(f"{c}:实盘{v}≠目标{t}" for c, (v, t) in list(drift.items())[:5])
                logger.warning(f"[对账未收敛] {dmsg}（等待下次对账修正，不自动重试）")
                send_alert(f"[{strategy_id}] 对账未收敛 {len(drift)}只: {dmsg}", level="warning")
        except Exception as e:
            logger.warning(f"对账复查失败: {e}")

        msg = (f"[{strategy_id}] 调仓完成 ({sig.get('signal_date')})\n"
               f"卖出 {len(results['sells'])} 笔 / "
               f"买入 {len(results['buys'])} 笔 / "
               f"风控拦截 {len(results['blocked'])} 笔")
        logger.info(msg)
        send_alert(msg)
        return results

    # ── 权重接口（兼容旧代码，策略层可直接调用）─────────────────────

    def rebalance(
        self,
        strategy_id: str,
        target_weights: dict[str, float],
        current_price: dict[str, float],
    ) -> dict:
        """按目标权重调仓（适用于需要实时计算手数的场景）。"""
        gw, account = self._build_gateway(strategy_id)
        positions   = self.client.get_positions()
        total       = account["total_assets"]

        target_shares = {
            code: (lambda lot: int((total * w / current_price[code]) // lot) * lot)(
                200 if str(code).startswith("688") else MIN_LOT
            )
            for code, w in target_weights.items()
            if current_price.get(code)
        }

        all_codes = set(target_shares) | set(positions)
        sells = [(c, positions[c]["volume"] - target_shares.get(c, 0), current_price.get(c, 0))
                 for c in all_codes if positions.get(c, {}).get("volume", 0) > target_shares.get(c, 0)
                 and current_price.get(c)]
        buys  = [(c, target_shares.get(c, 0) - positions.get(c, {}).get("volume", 0), current_price.get(c, 0))
                 for c in all_codes if target_shares.get(c, 0) > positions.get(c, {}).get("volume", 0)
                 and current_price.get(c)]

        results = {"sells": [], "buys": [], "blocked": []}
        for code, shares, price in sells:
            ok, reason = gw.check(strategy_id, code, "sell", shares, price)
            if ok:
                results["sells"].append({"code": code, "order_id":
                    self.client.place_order(code, "sell", shares, price * 0.998)})
            else:
                results["blocked"].append({"code": code, "direction": "sell", "reason": reason})
        for code, shares, price in buys:
            ok, reason = gw.check(strategy_id, code, "buy", shares, price)
            if ok:
                results["buys"].append({"code": code, "order_id":
                    self.client.place_order(code, "buy", shares, price * 1.002)})
            else:
                results["blocked"].append({"code": code, "direction": "buy", "reason": reason})

        send_alert(f"[{strategy_id}] 调仓: 卖{len(results['sells'])} 买{len(results['buys'])} "
                   f"拦截{len(results['blocked'])}")
        return results
