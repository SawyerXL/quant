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
SHARES = 100
OFFSET = 0.92  # 现价−8%: 主板跌停−10% 内, 不可能成交
STRATEGY = "track_a"
CAPITAL = 1_000_000


def _in_session() -> bool:
    hhmm = datetime.now().strftime("%H:%M")
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "14:57")


def rt_price(code: str) -> float:
    exch = "sh" if code.startswith(("5", "6", "9")) else "sz"
    url = f"http://hq.sinajs.cn/list={exch}{code}"
    req = urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn"})
    data = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
    return float(data.split('"')[1].split(",")[3])


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
    now = rt_price(CODE)
    far = round(now * OFFSET, 2)
    gw.state["limit_prices"] = _limit_prices([CODE], {CODE: now},
                                             _build_st_map())
    ok, reason = gw.check(STRATEGY, CODE, "buy", SHARES, far)
    result.update({"price_now": now, "price_far": far,
                   "risk_ok": ok, "risk_reason": reason})
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
            time.sleep(2)
            orders = trader.client.get_today_orders()
            mine = [o for o in orders if o.get("order_id") == oid]
            filled = mine[0].get("filled", -1) if mine else -1
            status = mine[0].get("status", -1) if mine else -1
            result.update({"cancel_ok": cancelled, "filled": filled,
                           "order_status": status})
            if cancelled and filled == 0:
                result["status"] = "PASS"
            else:
                result["status"] = "CHECK_MANUAL"
                logger.error(f"撤单未确认: cancel={cancelled} filled={filled} "
                             f"status={status} → 必须人工在 QMT 核查该委托!")
    out = Path("logs") / f"verify_submit_path_{datetime.now():%Y%m%d}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
