"""
QMT 当日订单导出 — 静跑期成本实测取数（2026-09-06 建）
在 Windows 上运行（或由 Linux 经 2222 隧道远程驱动）：
    python scripts/dump_qmt_orders.py

把账户当日全部委托（含成交价/成交时间/备注）导出到 logs/qmt_orders_latest.json，
供 Linux 侧 scripts/measure_execution_costs.py 计算逐笔成交成本（§6.8 18bp 裁决）。

为什么独立成脚本：现有 execution_result 只记买入滑点且无成交价——卖出侧与
MA10 触发单（903% 换手的 93% 来源）从未回流 Linux。独立脚本不改
fetch_and_execute 执行链（9/7 清仓前夜零改动风险）。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from loguru import logger

OUT = ROOT / "logs" / "qmt_orders_latest.json"


def _get_attr(o, name, default=""):
    return getattr(o, name, default) or default


def main():
    from execution.qmt_client import get_client

    c = get_client()
    # 底层 query_stock_orders 拿成交价/时间等富字段；
    # 方向沿用 qmt_client 口径（order_type == STOCK_BUY），不重造
    basic = {o["order_id"]: o for o in (c.get_today_orders() or [])}
    raw = c.trader.query_stock_orders(c.account, cancelable_only=False) or []
    orders = []
    for o in raw:
        b = basic.get(_get_attr(o, "order_id", 0), {})
        orders.append({
            "order_id":    _get_attr(o, "order_id", 0),
            "code":        str(_get_attr(o, "stock_code", "")).split(".")[0],
            "direction":   b.get("direction", ""),
            "shares":      int(_get_attr(o, "order_volume", 0)),
            "limit_price": float(_get_attr(o, "price", 0) or 0),
            "fill_price":  float(_get_attr(o, "traded_price", 0) or 0),
            "filled":      int(_get_attr(o, "traded_volume", 0) or 0),
            "status":      _get_attr(o, "order_status", 0),
            "order_time":  str(_get_attr(o, "order_time", "")),
            "remark":      str(_get_attr(o, "order_remark", "")),
        })

    payload = {
        "signal_date": datetime.now().strftime("%Y%m%d"),
        "dumped_at":   datetime.now().isoformat(),
        "order_count": len(orders),
        "orders":      orders,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"订单导出: {len(orders)}笔 → {OUT}")


if __name__ == "__main__":
    main()
