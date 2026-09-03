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

# 各track名义资金(2026-09-01 配比定案: 股票40万+CB60万)。信号文件自带capital
# 时以信号为准，此处仅作信号缺失时的兜底。
TRACK_CAPITAL_FALLBACK = {"track_a": 1_000_000, "track_b": 400_000, "track_cb": 600_000}

# 板块涨跌停幅度(涨停禁买/跌停禁卖推算用); ST 单独 5%
_BOARD_BANDS = {("60", "00"): 0.10, ("30", "68"): 0.20, ("8", "4", "92"): 0.30}


def _norm(code) -> str:
    """6位代码归一化: '600276.SH' → '600276'。"""
    return str(code).split(".")[0]


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


def _build_st_map() -> set[str]:
    """ST股集合(涨跌停5%档)。"""
    info = load_meta("stock_info_full")
    if info.empty or "is_st" not in info.columns:
        return set()
    return {str(c) for c, st in zip(info["code"], info["is_st"]) if st}


def _limit_prices(codes: list[str], ref_prices: dict, st_codes: set) -> dict:
    """推算涨跌停价: 信号价=昨收, 涨跌停=昨收×(1±幅度)。信号价缺失的code跳过
    (风控端fail-closed拦截, 但sell路径的cost_price兜底会先补ref)。"""
    out = {}
    for code in codes:
        ref = ref_prices.get(code)
        if not ref or ref <= 0:
            continue
        if code in st_codes:
            band = 0.05
        else:
            band = next((b for pfx, b in _BOARD_BANDS.items()
                         if code.startswith(pfx)), 0.10)
        out[code] = {"up": ref * (1 + band), "down": ref * (1 - band)}
    return out


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

        # 本track全资金口径(风控NAV/分母基数): 信号"capital"是全资金(CB),
        # "effective_capital"是档位缩放后的目标市值——绝不能用它做NAV基准
        # (9/3事故: 0.5档50万 vs nav_high 100万 → 假回撤48% → 熔断误拦34笔)
        capital = float(sig.get("capital")
                        or TRACK_CAPITAL_FALLBACK.get(strategy_id, 400_000))
        if not sig.get("capital"):
            logger.warning(f"[{strategy_id}] 信号无capital字段, "
                           f"风控按全资金兜底 {capital:,.0f}")

        # 熊市信号：清仓
        if sig.get("regime") == "bear":
            return self._execute_bear(strategy_id, sig, capital)

        # 牛市信号：按 shares 字段直接执行差量调仓
        return self._execute_bull(strategy_id, sig, capital)

    # ── 内部执行逻辑 ─────────────────────────────────────────────────

    def _build_gateway(self, strategy_id: str, capital: float) -> tuple[RiskGateway, dict]:
        """构建风控网关（2026-09-02 修复: 百分比口径改按track名义资金计算）。

        QMT仿真户 total_assets≈4837万(种子资金污染值), 用它做分母会让
        单票5%/行业30%/熔断25%/现金检查全部永不触发。正确口径:
          total_assets = 本track名义资金(信号capital, 缺省按TRACK_CAPITAL_FALLBACK)
          cash         = 名义资金 - 持仓市值
          current_nav  = 名义资金 + Σ(市值-成本)  (notional MTM)
        nav_high 写入加 sanity 带: 名义资金±50% 之外拒绝写入+告警, 防止
        污染值永久毒化熔断线。
        """
        account   = self.client.get_account_info()
        positions = {_norm(k): v for k, v in self.client.get_positions().items()
                     if self._in_scope(k, strategy_id)}
        mv_total  = sum(p.get("market_value", 0) for p in positions.values())
        cost_total = sum(p.get("volume", 0) * p.get("cost_price", 0)
                         for p in positions.values())
        current_nav = capital + (mv_total - cost_total)

        stored_high = _load_nav_high(strategy_id)
        nav_high    = max(stored_high, current_nav)
        # sanity: 名义资金±50%带之外视为数据污染, 只告警不写入
        if 0.5 * capital <= current_nav <= 1.5 * capital:
            _save_nav_high(strategy_id, current_nav)
        else:
            send_alert(
                f"[{strategy_id}] nav_high 拒绝写入: current_nav={current_nav:,.0f} "
                f"超出 sanity 带(名义资金{capital:,.0f}±50%), 熔断线沿用 {stored_high:,.0f}",
                level="error")

        return RiskGateway({
            "total_assets":   capital,
            "cash":           max(0.0, capital - mv_total),
            "positions":      {c: p.get("market_value", 0) for c, p in positions.items()},
            "nav_high":       nav_high,
            "current_nav":    current_nav,
            "strategy_positions": {
                strategy_id: {c: p.get("market_value", 0) for c, p in positions.items()}
            },
            "industry_map":   _build_industry_map(),
        }), account

    def _execute_bear(self, strategy_id: str, sig: dict, capital: float) -> dict:
        """熊市：卖出本track所有持仓（CB清仓只卖转债，股票清仓只卖股票）。

        2026-09-02 修复: ① 旧版用 cost_price×0.998 挂限价卖——熊市股价普遍
        低于成本, 卖限价高于市价永不撮合, 清仓静默失败; 改市价单。
        ② 分笔≤10万防错单红线。③ key归一化后过风控。
        """
        positions = {_norm(k): v for k, v in self.client.get_positions().items()
                     if self._in_scope(k, strategy_id)}
        ref_prices = {c: float(p.get("cost_price", 0)) for c, p in positions.items()}
        gw, _ = self._build_gateway(strategy_id, capital)
        gw.state["limit_prices"] = _limit_prices(list(positions), ref_prices,
                                                 _build_st_map())
        results = {"sells": [], "buys": [], "blocked": []}

        MAX_ORD = 100_000
        for code, pos in positions.items():
            shares = pos.get("volume", 0)
            price  = pos.get("cost_price", 1.0)
            if shares <= 0:
                continue
            remaining = shares
            while remaining > 0:
                q = min(remaining, int(MAX_ORD / max(price, 0.01)))
                if q <= 0:
                    q = remaining
                ok, reason = gw.check(strategy_id, code, "sell", q, price)
                if not ok:
                    results["blocked"].append({"code": code, "direction": "sell",
                                               "reason": reason})
                    break
                # 2026-09-03: 熊市清仓同口径——跌停价限价单(市价单被
                # 仿真柜台废单实测, 跌停价限价=立即市价成交且永远合法)
                limits = gw.state.get("limit_prices", {}).get(code)
                order_px = limits["down"] if limits else round(price * 0.9, 2)
                oid = self.client.place_order(code, "sell", q, order_px)
                results["sells"].append({"code": code, "shares": q, "order_id": oid})
                remaining -= q

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

    def _execute_bull(self, strategy_id: str, sig: dict, capital: float) -> dict:
        """目标仓位 diff 式下单（2026-08-31 幂等化改造）。

        只下"目标持仓 − 实际持仓"的差量：重复执行/重试会收敛到同一状态，
        杜绝增量式指令重发导致的重复下单与仓位漂移。先卖后买。
        2026-09-02 修复: ① 风控状态用归一化key(单票/行业检查恢复生效);
        ② 持仓在holdings但无shares条目 → 跳过卖出+告警(fail-safe, 防9/1
        型误清仓); ③ 限价检查实装; ④ 对账复查key归一化(消除天天假报差异)。
        """
        # 统一归一化: 信号code无后缀('600276'), positions/orders带后缀('600276.SH')
        # 2026-09-01 两次事故根因: key格式错位导致diff把持仓当0 → 重复下单
        def _norm(c):
            return str(c).split(".")[0]
        raw_positions = self.client.get_positions()
        positions = {_norm(k): v for k, v in raw_positions.items()
                     if self._in_scope(k, strategy_id)}
        # 成交回报延迟修正: 未终态委托的未成交净额并入持仓口径
        # (已下未回报的买单不再被当缺失)。2026-09-03 修复: 原把废单(57)/
        # 已撤/全成单的整单量都计入 → 32笔废单被当成"已买入" → 重跑时
        # 买入差量=0、部署静默漏买; 正确口径只计未终态(50已报/52部成/
        # 54部成待撤)的 shares-filled 净额。
        try:
            OPEN_STATUS = (50, 52, 54)
            for o in self.client.get_today_orders():
                code = _norm(o.get("code"))
                if not self._in_scope(code, strategy_id):
                    continue
                if o.get("status") not in OPEN_STATUS:
                    continue
                d = o.get("direction")
                net = max(0, o.get("shares", 0) - o.get("filled", 0))
                cur = positions.setdefault(code, {"volume": 0, "market_value": 0})
                if d == "buy":
                    cur["volume"] += net
                elif d == "sell":
                    cur["volume"] -= net
        except Exception:
            pass
        target_shares = {_norm(k): v for k, v in sig.get("shares", {}).items()}
        holdings      = {_norm(c) for c in sig.get("holdings", [])}
        sell_set      = {_norm(c) for c in sig.get("sell", [])}
        ref_prices    = {_norm(k): v for k, v in sig.get("prices", {}).items()}
        gw, account = self._build_gateway(strategy_id, capital)
        # 限价检查数据: 信号价推算涨跌停(信号价=昨收); 无信号价的用成本价兜底
        check_prices = dict(ref_prices)
        for c in set(target_shares) | set(sell_set) | set(positions):
            if c not in check_prices:
                cp = positions.get(c, {}).get("cost_price", 0)
                if cp and cp > 0:
                    check_prices[c] = float(cp)
        gw.state["limit_prices"] = _limit_prices(list(check_prices), check_prices,
                                                 _build_st_map())
        results = {"sells": [], "buys": [], "blocked": []}

        # ── 卖出差量：清仓票全卖 / 目标<实际 补卖差量 ──
        missing_shares = []
        for code in sorted(set(positions) | set(target_shares)):
            cur = positions.get(code, {}).get("volume", 0)
            if code in sell_set or (cur > 0 and code not in holdings):
                sell_qty = cur                      # 清仓
            elif code in holdings and code not in target_shares:
                # fail-safe: 持仓在目标池但无目标手数 → 信号侧数据缺口,
                # 不得解释为"目标=0"全额卖出(2026-09-01 事故路径)
                if cur > 0:
                    missing_shares.append(code)
                sell_qty = 0
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
                # 2026-09-03 卖出定价: 跌停价限价单——立即以市价成交且
                # 永远合法(原×0.998下跌日挂不上; 市价单被仿真柜台废单
                # 301526实测57)。跌停价=昨收×(1-板块幅度), 从风控limit
                # 表取; 触及跌停的票已被风险检查拦截, 不会排队
                limits = gw.state.get("limit_prices", {}).get(code)
                order_px = limits["down"] if limits else round(price * 0.9, 2)
                oid = self.client.place_order(code, "sell", q, order_px)
                results["sells"].append({"code": code, "shares": q,
                                         "price": order_px, "order_id": oid})
                logger.info(f"卖出 {code} {q}股 (限价@跌停{order_px}, 参考@{price:.2f})")
                remaining -= q

        if missing_shares:
            msg = (f"[{strategy_id}] 持仓无目标手数, 跳过卖出 {len(missing_shares)}只 "
                   f"(fail-safe): {missing_shares[:6]} —— 信号生成器shares未补齐, "
                   f"请检查 daily_signal 数据完整性")
            logger.error(msg)
            send_alert(msg, level="error")

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
            actual = {_norm(k): v for k, v in self.client.get_positions().items()
                      if self._in_scope(k, strategy_id)}
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
        capital: float | None = None,
    ) -> dict:
        """按目标权重调仓（适用于需要实时计算手数的场景）。
        2026-09-02 修复: positions/target 两侧 key 归一化(旧版后缀错位会
        把全部持仓当缺失→整仓换血), 资金口径按名义capital。
        """
        cap = capital or TRACK_CAPITAL_FALLBACK.get(strategy_id, 400_000)
        gw, _ = self._build_gateway(strategy_id, cap)
        positions   = {_norm(k): v for k, v in self.client.get_positions().items()
                       if self._in_scope(k, strategy_id)}
        target_weights = {_norm(k): v for k, v in target_weights.items()}
        current_price  = {_norm(k): v for k, v in current_price.items()}
        gw.state["limit_prices"] = _limit_prices(
            list(current_price), current_price, _build_st_map())

        target_shares = {
            code: (lambda lot: int((cap * w / current_price[code]) // lot) * lot)(
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
                # 2026-09-03 卖出定价: 跌停价限价单(与_execute_bull同口径)
                limits = gw.state.get("limit_prices", {}).get(code)
                order_px = limits["down"] if limits else round(price * 0.9, 2)
                results["sells"].append({"code": code, "order_id":
                    self.client.place_order(code, "sell", shares, order_px)})
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
