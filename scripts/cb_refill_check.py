"""5笔补仓核对 (2026-09-23): 委托/成交/持仓三层 vs 清单逐位比对。
清单=logs/cb_refill_plan_20260923.json (用户深夜锁死)。
判据: ①委托数=5 ②成交张数=委托张数逐只 ⑤无废单 ③(次日)持仓变化=清单。
用法: python scripts/cb_refill_check.py [--orders logs/qmt_orders_20260923.json]
      [--pos logs/qmt_positions_latest.json] [--pre logs/qmt_positions_20260922.json]
"""
import argparse
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
PLAN_FILE = ROOT / "logs" / "cb_refill_plan_20260923.json"


def load_orders(p: Path) -> list:
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("orders", [])


def load_pos(p: Path) -> dict:
    d = json.loads(p.read_text(encoding="utf-8"))
    return {str(k).split(".")[0]: v for k, v in d.get("positions", {}).items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=str, default=None)
    ap.add_argument("--pos", type=str, default="logs/qmt_positions_latest.json")
    ap.add_argument("--pre", type=str, default=None)
    args = ap.parse_args()

    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))["plan"]
    codes = set(plan)

    print(f"===== CB补仓核对 {date.today()} 清单 {plan} =====")
    orders_path = Path(args.orders) if args.orders else None
    if orders_path is None or not orders_path.exists():
        print("  (无订单文件, 只做持仓侧检查)")
    else:
        orders = load_orders(orders_path)
        buys = [o for o in orders if o["direction"] == "buy"
                and o.get("code") in codes]
        print(f"  判据①: 当日买入委托 {len(buys)} 笔 (清单 {len(codes)} 笔)"
              f" → {'PASS' if len(buys) == len(codes) else 'FAIL'}")
        ok_all = True
        for c in sorted(codes):
            os_ = [o for o in buys if o["code"] == c]
            if not os_:
                print(f"    {c}: 无委托 → FAIL")
                ok_all = False
                continue
            for o in os_:
                st = o["status"]
                line = (f"    {c}: 委托{o['shares']} 成交{o['filled']} "
                        f"状态{st} 限价{o['limit_price']}")
                qty_ok = o["shares"] == plan[c]
                fill_ok = o["filled"] == o["shares"]
                rej_ok = st != 57
                verdict = "PASS" if (qty_ok and fill_ok and rej_ok) else "FAIL"
                ok_all &= verdict == "PASS"
                print(f"      {line} → {verdict}"
                      f" (张数={qty_ok} 成交={fill_ok} 无废单={rej_ok})")
        rej = [o for o in buys if o["status"] == 57]
        print(f"  判据⑤: 废单 {len(rej)} 笔 → {'PASS' if not rej else 'FAIL'}")

    pos_path = Path(args.pos)
    if pos_path.exists():
        pos = load_pos(pos_path)
        print("  当日持仓:")
        for c in sorted(codes):
            v = pos.get(c, {}).get("volume", 0)
            print(f"    {c}: {v} 张")
        if args.pre and Path(args.pre).exists():
            pre = load_pos(Path(args.pre))
            print("  判据③(持仓变化 vs 前快照):")
            for c in sorted(codes):
                d = pos.get(c, {}).get("volume", 0) - pre.get(c, {}).get("volume", 0)
                ok = d == plan[c]
                print(f"    {c}: {pre.get(c, {}).get('volume', 0)}→"
                      f"{pos.get(c, {}).get('volume', 0)} 差{d}"
                      f" (清单{plan[c]}) → {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
