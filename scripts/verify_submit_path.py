"""执行链提交+撤单路径验证 (2026-09-17, Git-Sync 修复后一次性工具)。

验证同步后的 Windows 代码能走完真实提交路径:
  Trader._build_gateway(真实风控六项) → place_order(normalize_code
  + tick 归一 + order_stock) → cancel_order → 委托状态复核。
用不可成交限价(现价−8%, 主板跌停−10% 内)买 100 股, 不产生持仓。
约束: ①必须在交易时段内(非时段直接拒跑) ②撤单确认成功+filled=0 才算
PASS, 否则状态=CHECK_MANUAL 必须人工处置 ③走 trader 真实调用非手点。
用法(Windows): python scripts/verify_submit_path.py [代码=600000]
输出: logs/verify_submit_path_YYYYMMDD.json
"""
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from execution.trader import Trader, _build_st_map, _limit_prices

CODE = sys.argv[1] if len(sys.argv) > 1 else "600000"
SHARES = int(sys.argv[2]) if len(sys.argv) > 2 else 100
OFFSET = 0.92  # 现价−8%: 主板跌停−10% 内, 不可能成交
STRATEGY = "track_a"
CAPITAL = 1_000_000


def _in_session() -> bool:
    hhmm = datetime.now().strftime("%H:%M")
    # 10:00 后: 避开开盘第一分钟盘口不稳定(09-31 现价基准可能异常 tick)
    return ("10:00" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "14:57")


def rt_quote(code: str) -> dict:
    """新浪实时: {prev_close(昨收), now(现价)}"""
    exch = "sh" if code.startswith(("5", "6", "9")) else "sz"
    url = f"http://hq.sinajs.cn/list={exch}{code}"
    req = urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn"})
    data = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
    parts = data.split('"')[1].split(",")
    return {"prev_close": float(parts[2]), "now": float(parts[3])}


def safe_far_price(code: str) -> tuple[float, dict]:
    """委托价兜底: max(现价×0.92, 跌停价×1.01), 且必须 < 现价×0.98。
    基准价异常时拒绝提交(SKIP)而不是提交一张可能被拒的单。"""
    q = rt_quote(code)
    now, prev = q["now"], q["prev_close"]
    if now <= 0 or prev <= 0:
        return -1, q
    down = round(prev * 0.90, 2)      # 主板跌停价(近似)
    far = max(round(now * OFFSET, 2), round(down * 1.01, 2))
    if not (far < now * 0.98):
        return -1, q
    return far, q


def main():
    ts = datetime.now().isoformat()
    result = {"code": CODE, "shares": SHARES, "ts": ts}
    if not _in_session():
        result["status"] = "NOT_IN_SESSION"
        result["note"] = "非交易时段, 拒绝提交(约束①)"
        print(json.dumps(result, ensure_ascii=False))
        return

    trader = Trader()
    gw, _account = trader._build_gateway(STRATEGY, CAPITAL)
    far, quote = safe_far_price(CODE)
    result.update({"price_now": quote.get("now"),
                   "prev_close": quote.get("prev_close"),
                   "price_far": far})
    if far < 0:
        result["status"] = "SKIP"
        result["note"] = ("价格基准异常或兜底断言不满足"
                          "(委托价须<现价×0.98), 拒绝提交")
        logger.error(result["note"])
        print(json.dumps(result, ensure_ascii=False))
        return
    gw.state["limit_prices"] = _limit_prices([CODE], {CODE: quote["now"]},
                                             _build_st_map())
    ok, reason = gw.check(STRATEGY, CODE, "buy", SHARES, far)
    result["risk_ok"], result["risk_reason"] = ok, reason
    if not ok:
        result["status"] = "BLOCKED_AT_RISK"
        logger.error(f"风控拦截: {reason} (提交路径未走到)")
    else:
        oid = trader.client.place_order(CODE, "buy", SHARES, far)
        result["order_id"] = oid
        if oid < 0:
            result["status"] = "ORDER_FAILED"
            logger.error("委托提交失败 order_id=-1")
        else:
            time.sleep(2)
            cancelled = trader.client.cancel_order(oid)
            # 撤单确认: 轮询可撤列表最多30秒(挂单过夜比测试失败更糟)
            deadline = time.time() + 30
            still_open = True
            while time.time() < deadline:
                open_ids = {o.order_id for o in
                            trader.client.trader.query_stock_orders(
                                trader.client.account, cancelable_only=True)
                            or []}
                if oid not in open_ids:
                    still_open = False
                    break
                time.sleep(5)
            orders = trader.client.get_today_orders()
            mine = [o for o in orders if o.get("order_id") == oid]
            filled = mine[0].get("filled", -1) if mine else -1
            status = mine[0].get("status", -1) if mine else -1
            result.update({"cancel_ok": cancelled, "filled": filled,
                           "order_status": status,
                           "still_open": still_open})
            if cancelled and not still_open and filled == 0:
                result["status"] = "PASS"
            else:
                result["status"] = "OPEN_ORDER"
                try:
                    from monitoring.alerts import p0_alert
                    p0_alert("验证委托未撤净",
                             f"{CODE} 验证单 oid={oid} 30秒内未确认撤净"
                             f"(cancel={cancelled} open={still_open} "
                             f"filled={filled}), 必须人工核查")
                except Exception as e:
                    logger.error(f"p0 告警发送失败: {e}")
                logger.error(f"撤单未确认: cancel={cancelled} "
                             f"still_open={still_open} filled={filled} "
                             f"status={status} → 必须人工在 QMT 核查!")
    out = Path("logs") / f"verify_submit_path_{datetime.now():%Y%m%d}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
