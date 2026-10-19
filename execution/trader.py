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

# 熊市清仓豁免清单(2026-09-15): 与 scripts/cash_sweep.py BOND_ETF 同源——
# sweep(短债现金管理)拥有的持仓不进熊市清仓, 否则 09-14 型"清仓卖底仓+
# sweep 买回"的相消亏损重演。改动必须两处同步。
BEAR_EXEMPT_CODES = {"511360"}

# 各track名义资金(2026-09-01 配比定案: 股票40万+CB60万)。信号文件自带capital
# 时以信号为准，此处仅作信号缺失时的兜底。
TRACK_CAPITAL_FALLBACK = {"track_a": 1_000_000, "track_b": 400_000, "track_cb": 600_000}

# 板块涨跌停幅度(涨停禁买/跌停禁卖推算用); ST 单独 5%
_BOARD_BANDS = {("60", "00"): 0.10, ("30", "68"): 0.20, ("8", "4", "92"): 0.30}

# 未终态委托状态(2026-10-19 实锤): 原 (50,52,54) 把 54=已撤当未终态,
# 撤单的未成交量被并进持仓 → 200 实持 + 200 撤单 = 400 → 派生价 mv/vol
# 腰斩(115→57.6) → 链自动挂 56.44 卖 127068(当日刚买入的券)。QMT 状态码:
# 48 未报/49 待报/50 已报/51 已报待撤/52 部成待撤/55 部成 = 未终态;
# 53 部撤/54 已撤/56 已成/57 废单 = 终态。保守口径: 除终态外都算未终态
# (多算=少下单, 漏算=重复下单, 后者危险)。
_TERMINAL_ORDER_STATUS = {53, 54, 56, 57}


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


def _round_cb_lot(qty: int) -> int:
    """CB买入张数取整到10的整数倍(半进位: 47→50, 5→10, 4→0)。
    2026-09-22 废单调查: 差量=信号目标−实际持仓, 持仓可能为奇数张
    (09-01乱序买卖残留), 柜台对买入方向拒非整手(卖出零头一次性合法
    ——09-01五笔奇数卖出成交实锤买卖不对称)。取整后差量幂等收敛
    (目标±5张内不再下单)。"""
    return (int(qty) + 5) // 10 * 10


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
        2026-09-15 豁免(闸门十二落地): sweep 管理的持仓不进熊市清仓——
        09-14 假深熊清仓卖出 511360(打新底仓)的直接原因=豁免额未传导到
        trader, "拦截0笔"=豁免清单为空(闸门十四第八例)。豁免码与
        scripts/cash_sweep.py BOND_ETF 同源, 改动须同步。
        """
        positions = {_norm(k): v for k, v in self.client.get_positions().items()
                     if self._in_scope(k, strategy_id)}
        exempted = [c for c in positions if c in BEAR_EXEMPT_CODES]
        positions = {c: v for c, v in positions.items() if c not in BEAR_EXEMPT_CODES}
        ref_prices = {c: float(p.get("cost_price", 0)) for c, p in positions.items()}
        gw, _ = self._build_gateway(strategy_id, capital)
        gw.state["limit_prices"] = _limit_prices(list(positions), ref_prices,
                                                 _build_st_map())
        results = {"sells": [], "buys": [], "blocked": []}
        # 豁免计入拦截计数: "拦截 N 笔"必须反映全部未卖出的持仓(2026-09-15)
        for c in exempted:
            results["blocked"].append({"code": c, "direction": "sell",
                                       "reason": "豁免标的(sweep管理)"})

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
                # 2026-09-04: 熊市清仓同口径——实时价×0.98限价单
                # (市价单被仿真柜台废单实测; 跌停价依赖昨收, Windows
                # 拿不到实时昨收, 用持仓市值/股数的实时价最稳)
                if pos.get("volume") and pos.get("market_value"):
                    live_px = float(pos["market_value"]) / pos["volume"]
                    if live_px > 0:
                        price = live_px
                oid = self.client.place_order(code, "sell", q, round(price * 0.98, 2))
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

    def compute_plan(self, strategy_id: str, sig: dict) -> dict:
        """差量计划(不下单, 2026-09-24 档位机制): 与 _execute_bull 完全同口径——
        卖=清仓票全卖/目标<实际减仓(CB<10张死区), 买=目标−持仓(CB取整10),
        当日废单买入跳过。输出逐笔 {code, direction, shares, price, est_amount},
        供档位分流/清单确认/调仓预览。不连风控、不下单。
        """
        def _norm(c):
            return str(c).split(".")[0]
        raw_positions = self.client.get_positions()
        positions = {_norm(k): v for k, v in raw_positions.items()
                     if self._in_scope(k, strategy_id)}
        try:
            today_orders = self.client.get_today_orders() or []
            rejected_buys = {
                _norm(o.get("code"))
                for o in today_orders
                if o.get("status") == 57 and o.get("direction") == "buy"
            }
            for o in today_orders:
                code = _norm(o.get("code"))
                if not self._in_scope(code, strategy_id):
                    continue
                if o.get("status") in _TERMINAL_ORDER_STATUS:
                    continue
                d = o.get("direction")
                net = max(0, o.get("shares", 0) - o.get("filled", 0))
                cur = positions.setdefault(code, {"volume": 0, "market_value": 0})
                if d == "buy":
                    cur["volume"] += net
                elif d == "sell":
                    cur["volume"] -= net
        except Exception:
            rejected_buys = set()
        target_shares = {_norm(k): v for k, v in sig.get("shares", {}).items()}
        holdings = {_norm(c) for c in sig.get("holdings", [])}
        sell_set = {_norm(c) for c in sig.get("sell", [])}
        ref_prices = {_norm(k): v for k, v in sig.get("prices", {}).items()}

        # 删笔清单(2026-10-14 实装, 十二/十三/十六轮规格): 人工删掉的单不
        # 自动补回(双向); 清单内券触发强制退出(信号 excluded)→不抑制+告警
        # (十三轮: 强制退出优先于清单, 代码职责=不静默排除+留痕)。
        from execution.manual_delete_list import load_list, suppression_sets
        dl = load_list()
        forced_exit = {_norm(c) for c in sig.get("excluded", {})}
        suppress_sells, suppress_buys, overrides = suppression_sets(dl, forced_exit)
        dl_reason = {(e.get("code"), e.get("direction")): e.get("reason", "")
                     for e in dl.get("entries", [])}
        for ov in overrides:
            logger.error(
                f"删笔清单券触发强制退出, 不抑制({ov['code']} {ov['direction']})")
            send_alert(f"[track_cb] 删笔清单券触发强制退出: {ov['code']} "
                       f"{ov['direction']} (清单原因: {ov['reason']})",
                       level="error")

        plan = {"sells": [], "buys": [], "skipped_rejected": [],
                "skipped_deleted": [], "missing_shares": []}
        for code in sorted(set(positions) | set(target_shares)):
            # 2026-09-29: 熊市清仓豁免传导到差量路径——BEAR_EXEMPT_CODES 的
            # sweep 管理持仓(511360 打新底仓)不得进卖出计划。09-15 豁免只落在
            # legacy _execute_bear, 档位机制的 compute_plan 漏带(09-29 A-track
            # 清单实锤"卖511360 6100")——档1/档2自动执行时=69.5万底仓被自动
            # 卖出的潜伏风险, 硬拦截不回退。
            if code in BEAR_EXEMPT_CODES:
                continue
            cur = positions.get(code, {}).get("volume", 0)
            if code in sell_set or (cur > 0 and code not in holdings):
                sell_qty = cur
            elif code in holdings and code not in target_shares:
                if cur > 0:
                    plan["missing_shares"].append(code)
                sell_qty = 0
            elif cur > target_shares.get(code, 0):
                sell_qty = cur - target_shares.get(code, 0)
            else:
                sell_qty = 0
            if sell_qty <= 0:
                continue
            if (strategy_id == "track_cb" and sell_qty < 10
                    and code not in sell_set and code in holdings):
                continue
            if code in suppress_sells:
                # 闸门十二: 跳过是事件——留痕(清单抑制人工删掉的卖单)
                plan["skipped_deleted"].append({
                    "code": code, "direction": "sell",
                    "reason": dl_reason.get((code, "sell"), "")})
                logger.warning(f"删笔清单抑制卖出 {code}(人工删除不自动补)")
                continue
            pos_live = positions.get(code, {})
            if pos_live.get("volume") and pos_live.get("market_value"):
                live_px = float(pos_live["market_value"]) / pos_live["volume"]
                price = (live_px if live_px > 0
                         else ref_prices.get(code)
                         or pos_live.get("cost_price", 1.0))
            else:
                price = ref_prices.get(code) or pos_live.get("cost_price", 1.0)
            plan["sells"].append({
                "code": code, "direction": "sell", "shares": sell_qty,
                "price": round(price, 2),
                "est_amount": round(sell_qty * price, 0),
                "held": cur, "target": target_shares.get(code, 0),
            })
        for code, tgt in target_shares.items():
            if code in sell_set:
                continue
            cur = positions.get(code, {}).get("volume", 0)
            buy_qty = tgt - cur
            if strategy_id == "track_cb":
                buy_qty = _round_cb_lot(buy_qty)
            if code in rejected_buys:
                plan["skipped_rejected"].append(
                    {"code": code, "direction": "buy",
                     "reason": "当日已废单, 不重试"})
                continue
            if code in suppress_buys:
                plan["skipped_deleted"].append({
                    "code": code, "direction": "buy",
                    "reason": dl_reason.get((code, "buy"), "")})
                logger.warning(f"删笔清单抑制买入 {code}(人工删除不自动补)")
                continue
            price = ref_prices.get(code, 0)
            if buy_qty <= 0 or price <= 0:
                continue
            plan["buys"].append({
                "code": code, "direction": "buy", "shares": buy_qty,
                "price": round(price, 2),
                "est_amount": round(buy_qty * price, 0),
                "held": cur, "target": tgt,
            })
        return plan

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
            today_orders = self.client.get_today_orders() or []
            # 2026-09-22: 当日已废单的买入不再重试(09-01多波重跑每波
            # 重复撞8只停牌券+1只仿真冻结券的废单; 废单是柜台判定的
            # "不可成交", 当日重试纯噪音)。卖出方向不受此限。
            rejected_buys = {
                _norm(o.get("code"))
                for o in today_orders
                if o.get("status") == 57 and o.get("direction") == "buy"
            }
            for o in today_orders:
                code = _norm(o.get("code"))
                if not self._in_scope(code, strategy_id):
                    continue
                if o.get("status") in _TERMINAL_ORDER_STATUS:
                    continue
                d = o.get("direction")
                net = max(0, o.get("shares", 0) - o.get("filled", 0))
                cur = positions.setdefault(code, {"volume": 0, "market_value": 0})
                if d == "buy":
                    cur["volume"] += net
                elif d == "sell":
                    cur["volume"] -= net
        except Exception:
            rejected_buys = set()
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
        results = {"sells": [], "buys": [], "blocked": [], "skipped_rejected": []}

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
            # 2026-09-22 死区: CB减仓差量<10张不交易——买入侧取整已隐式
            # 做到(47→50, 3→0), 卖出侧不加则"超配3张→天天卖3张"无谓换手;
            # 清仓路径不受限(奇数零头一次性卖出合法)
            if (strategy_id == "track_cb" and sell_qty < 10
                    and code not in sell_set and code in holdings):
                continue
            # 2026-09-04 卖单定价v3: 参考价用持仓实时市值/股数, 限价=实时×0.98。
            # 教训链: ×0.998挂不上(9/3) → 跌停价限价单(9/3)依赖昨收, 信号价
            # 过期时跌停估算低于真实跌停→废单(300408×2实锤: 旧价109.22已涨
            # 到115) → Windows无法访问新浪实时昨收(本地代理7892挂了)。
            # 实时×0.98: 在非跌停场景下恒在有效带内、低于市价立即成交、
            # 每次执行用最新市值刷新→自愈。跌停禁卖由风险层粗筛拦截。
            pos_live = positions.get(code, {})
            if pos_live.get("volume") and pos_live.get("market_value"):
                live_px = float(pos_live["market_value"]) / pos_live["volume"]
                if live_px > 0:
                    price = live_px
            else:
                price = ref_prices.get(code) or pos_live.get("cost_price", 1.0)
            gw.state["limit_prices"][code] = _limit_prices(
                [code], {code: price}, _build_st_map()).get(code)
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
                order_px = round(price * 0.98, 2)
                oid = self.client.place_order(code, "sell", q, order_px)
                results["sells"].append({"code": code, "shares": q,
                                         "price": order_px, "order_id": oid})
                logger.info(f"卖出 {code} {q}股 (限价@{order_px}, 实时@{price:.2f})")
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
            # 2026-09-22: CB买入张数取整到10整数倍(柜台拒非整手买入,
            # 09-21补仓47/57/72/74张全废; 卖出零头一次性合法, 故只限买入)
            if strategy_id == "track_cb":
                buy_qty = _round_cb_lot(buy_qty)
            # 2026-09-22: 当日已废单的买入不重试(停牌券/仿真冻结券
            # 每天重试=每天必废, 与闸门十二同族: 跳过必须是可见事件)
            if code in rejected_buys:
                results["skipped_rejected"].append(
                    {"code": code, "direction": "buy",
                     "reason": "当日已废单, 不重试(停牌/无行情类)"})
                continue
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

        if results["skipped_rejected"]:
            msg = (f"[{strategy_id}] 当日已废单跳过重试 {len(results['skipped_rejected'])}笔: "
                   f"{[r['code'] for r in results['skipped_rejected'][:8]]}")
            logger.warning(msg)
            send_alert(msg, level="warning")

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
                # 2026-09-04: 实时价×0.98限价单(与_execute_bull同口径,
                # current_price由调用方实时传入)
                results["sells"].append({"code": code, "order_id":
                    self.client.place_order(code, "sell", shares,
                                            round(price * 0.98, 2))})
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
