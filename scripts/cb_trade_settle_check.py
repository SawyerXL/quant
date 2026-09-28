"""CB 成交落账核对（通用，2026-09-28 为判据③建）。

判据③=次日(或当日收盘)快照持仓 = 前快照 + 成交结果（逐只逐位）。
判据②⑤=委托张数=成交张数、无废单（从订单文件自身读）。
与 cb_refill_check.py 的区别：预期差量由**订单成交回报**推导（+买/−卖），
不依赖人工锁定的计划文件——适用于任何有 orders+前后快照的交易日
（09-30 盘后三路对账首验同款可用）。
用法: python scripts/cb_trade_settle_check.py --orders logs/qmt_orders_20260924.json
      --pre logs/qmt_positions_20260923.json --post logs/qmt_positions_20260924.json
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent


def load_orders(p: Path) -> list:
    return json.loads(p.read_text(encoding="utf-8")).get("orders", [])


def load_pos(p: Path) -> dict:
    d = json.loads(p.read_text(encoding="utf-8"))
    return {str(k).split(".")[0]: v for k, v in d.get("positions", {}).items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", default=None)
    ap.add_argument("--pre", default=None)
    ap.add_argument("--post", default=None)
    ap.add_argument("--date", default=None,
                    help="YYYYMMDD: 自动找 logs/*orders_<date>.json 与前后快照")
    args = ap.parse_args()

    if args.date:
        from datetime import datetime, timedelta
        d = datetime.strptime(args.date, "%Y%m%d")
        ofs = sorted(ROOT.glob(f"logs/*orders_{args.date}.json"))
        if not ofs:
            raise SystemExit(f"无订单文件 logs/*orders_{args.date}.json")
        # 前快照: 往前找最近一个存在的持仓快照(≤15天)
        pre_p = None
        for back in range(1, 16):
            prev = (d - timedelta(days=back)).strftime("%Y%m%d")
            cands = sorted(ROOT.glob(f"logs/*positions_{prev}.json"))
            if cands:
                pre_p = cands[-1]
                break
        # 后快照: 当日与次日(若存在)——判据③=次日快照=昨日成交结果
        post_ps = sorted(ROOT.glob(f"logs/*positions_{args.date}.json")) + \
            sorted(ROOT.glob(f"logs/*positions_{(d + timedelta(days=1)).strftime('%Y%m%d')}.json"))
        if not post_ps:
            raise SystemExit(f"无后快照(当日或次日)")
        args.orders, args.pre = str(ofs[-1]), str(pre_p) if pre_p else None
        if not args.pre:
            raise SystemExit("前快照未找到(15 天内)")
        for pp in post_ps:
            run_check(Path(args.orders), Path(args.pre), Path(pp))
        return
    if not (args.orders and args.pre and args.post):
        raise SystemExit("需 --orders/--pre/--post 或 --date")
    run_check(Path(args.orders), Path(args.pre), Path(args.post))


def run_check(orders_p, pre_p, post_p):
    orders = load_orders(orders_p)
    pre = load_pos(pre_p)
    post = load_pos(post_p)

    # 预期差量: 按订单成交回报逐只累计(buy 加 filled, sell 减 filled)
    expect = {}
    rej = 0
    all_ok = True
    print(f"===== 成交落账核对 | 订单 {orders_p.name} "
          f"| 前 {pre_p.name} → 后 {post_p.name} =====")
    for o in orders:
        c, d, sh, fl, st = o["code"], o["direction"], o["shares"], o["filled"], o["status"]
        expect[c] = expect.get(c, 0) + (fl if d == "buy" else -fl)
        qty_ok = (sh == fl)
        rej_ok = (st != 57)
        ok = qty_ok and rej_ok
        all_ok &= ok
        print(f"  {c} {d:>4} 委托{sh} 成交{fl} 状态{st} → "
              f"{'PASS' if ok else 'FAIL'}(张数一致={qty_ok} 无废单={rej_ok})")
        if st == 57:
            rej += 1
    print(f"  判据②⑤: 委托=成交 {'PASS' if all_ok else 'FAIL'} | 废单 {rej} 笔")

    print("  判据③(持仓差量 vs 订单净额, 逐只):")
    codes = sorted(set(expect) | {c for c in pre if c in expect} | {c for c in post if c in expect})
    ok3 = True
    for c in codes:
        pv = pre.get(c, {}).get("volume", 0) or 0
        pt = post.get(c, {}).get("volume", 0) or 0
        d = pt - pv
        e = expect.get(c, 0)
        ok = (d == e)
        ok3 &= ok
        print(f"    {c}: {pv}→{pt} 差{d:+} (预期{e:+}) → {'PASS' if ok else 'FAIL'}")
    print(f"  判据③: {'PASS' if ok3 else 'FAIL'}"
          f"{' ← 落账与成交回报逐位一致' if ok3 else ' ← 见上, 逐只核对柜台原文'}")
    print(f"  判据④: 对账单(券商)待用户提供后人工比对")


if __name__ == "__main__":
    main()
