"""002028 负持仓伪影修复：买入 |volume| 股平掉空头（上限 399，10-29 安全窗口）。

为什么必须单独做：执行层把负持仓按 0 隔离，但 002028 的 −399 是仿真卖空
伪影；不修复则 100 万切换后股票名义 400k、单票 5% = 20k，这笔约 42.6k 的
平仓单会被 risk.py 永久拒绝。10-29 仍在仿真 1M 口径内（5% = 50k），是
切换前唯一安全窗口。默认 dry-run，--go 才实际下单。

所有订单过 RiskGateway；qmt_client.place_order 内部做 tick 归一。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # 锁文件/配置均按仓库根目录解析
os.environ.setdefault("ENV", "simulation")

CODE = "002028"
EXPECT_NEG = -399
STOCK_NOTIONAL = 1_000_000.0  # 仿真股票腿名义资金；切换 400k 后 5% 上限会拒绝本单
MAX_ORDER_VALUE = 100_000.0
LOCK = ROOT / "logs" / "fix_002028_lock.json"
MANUAL_LOCK = ROOT / "logs" / "manual_exec_lock.json"


def _norm_positions(raw: dict) -> dict:
    return {str(k).split(".")[0]: v for k, v in (raw or {}).items()}


def _tick() -> dict:
    """实时价/涨跌停；任一缺失即 fail-closed，不用推算值下单。"""
    from xtquant import xtdata

    from execution.qmt_client import _to_xt_code

    xt = _to_xt_code(CODE)
    xtdata.subscribe_quote(xt, period="tick")
    time.sleep(0.8)
    t = xtdata.get_full_tick([xt])[xt]

    def f(key):
        v = t.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0
        return float(v or 0)

    last, up, down = f("lastPrice"), f("upLimit"), f("downLimit")
    if last <= 0 or up <= 0 or down <= 0:
        raise RuntimeError(f"tick 不完整: last={last} up={up} down={down}")
    return {"last": last, "up": up, "down": down}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="实际下单；默认只检查")
    args = ap.parse_args()
    today = date.today().isoformat()

    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
            if info.get("date") == today:
                logger.error(f"今日已执行过修复，锁={info}；退出")
                return 2
        except Exception:
            logger.warning("修复锁不可解析，继续但请人工确认")
    if MANUAL_LOCK.exists():
        logger.error(f"人工执行锁在场（{MANUAL_LOCK}），拒绝下单")
        return 2

    from execution.qmt_client import get_client
    from execution.trader import Trader

    c = get_client()
    pos = _norm_positions(c.get_positions())
    vol = int(pos.get(CODE, {}).get("volume", 0))
    logger.info(f"{CODE} 当前 volume={vol}")
    if vol >= 0:
        logger.info(f"当前非负持仓（{vol}），无需修复；退出")
        return 0
    shares = -vol
    if shares > -EXPECT_NEG:
        logger.error(f"负持仓 {vol} 超过预期 {EXPECT_NEG}，疑似新伪影；不动")
        return 3

    tk = _tick()
    # 买方限价：last×1.005 保成交，但不超过涨停价−1 tick（涨停禁买另由风控拦）
    price = min(tk["up"] - 0.01, max(tk["last"], tk["down"] + 0.01) * 1.005)
    value = shares * price
    logger.info(
        f"tick last={tk['last']} up={tk['up']} down={tk['down']} "
        f"委托价={price:.3f} 金额={value:,.0f}"
    )
    if value > MAX_ORDER_VALUE:
        logger.error(f"单笔金额 {value:,.0f} > {MAX_ORDER_VALUE:,.0f}，停止")
        return 3
    if tk["last"] >= tk["up"] - 0.005:
        logger.error("涨停禁买（红线），停止")
        return 3

    t = Trader()
    gw, _ = t._build_gateway("track_a", STOCK_NOTIONAL)
    gw.state["limit_prices"] = {CODE: {"up": tk["up"], "down": tk["down"]}}
    ok, reason = gw.check("track_a", CODE, "buy", shares, price)
    logger.info(f"风控: {ok} {reason}")
    if not ok:
        return 4

    if not args.go:
        logger.info("dry-run：未下单（--go 才执行）")
        return 0

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(
        json.dumps(
            {"date": today, "time": time.strftime("%H:%M:%S"), "state": "placing"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    oid = c.place_order(CODE, "buy", shares, price)
    if oid is None or oid < 0:
        LOCK.unlink(missing_ok=True)
        logger.error(f"下单失败 oid={oid}，已移除锁")
        return 5
    LOCK.write_text(
        json.dumps(
            {
                "date": today,
                "time": time.strftime("%H:%M:%S"),
                "state": "placed",
                "order_id": oid,
                "price": price,
                "shares": shares,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info(f"已下单 order_id={oid}；等待回报")
    time.sleep(5)
    try:
        orders = [
            o for o in c.get_today_orders()
            if str(o.get("code", "")).split(".")[0] == CODE
        ]
        logger.info(f"当日委托: {orders}")
        pos2 = _norm_positions(c.get_positions())
        logger.info(f"回报后 {CODE} volume={pos2.get(CODE, {}).get('volume', 0)}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回报查询失败: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
