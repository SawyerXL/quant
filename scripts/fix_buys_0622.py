"""
6/22 开盘补单修正：上浮限价补齐 6/15-6/18 未成交的买单。
病根 = 固定限价锚在过期信号价，牛市跳空打不进。改为锚定 xtdata 实时价 + 上浮，并加三道护栏。

护栏：
  1) 涨停禁买（CLAUDE.md 红线）——开盘已封/逼近涨停的直接跳过；
  2) 追太远不追——现价较信号价已涨超 MAX_CHASE 的跳过（防再吃一个 600176 +28%）；
  3) 单笔不超 5万——超限自动削减股数（CLAUDE.md 防错单）。

用法（Windows QMT 服务器）：
  python scripts/fix_buys_0622.py          # DRY-RUN，只打印计划不下单
  python scripts/fix_buys_0622.py --go     # 实际下单
"""
import os, sys, json
os.environ["ENV"] = "simulation"
sys.path.insert(0, "H:/quant")
from execution.qmt_client import get_client, _to_xt_code

# ---- 可调参数（改动需记录原因） ----
BUFFER        = 0.01     # 上浮 1%：限价 = 实时价 ×1.01，保证开盘吃得进又不裸市价
MAX_CHASE     = 0.08     # 现价较信号价涨超 8% 即视为追高，放弃这一档
MAX_ORDER_AMT = 50_000   # 单笔金额上限（防错单）
LOT           = 100

GO = "--go" in sys.argv
SIG = json.loads(open("H:/quant/data_store/meta/signal_a_latest.json", "r", encoding="utf-8").read())
TARGETS = ["002155", "600378", "600999", "603156", "002008"]


def live_quote(code):
    """返回 (last, high_limit)；xtdata 不可用时返回 (None, None)。"""
    try:
        from xtquant import xtdata
        xt = _to_xt_code(code)
        xtdata.subscribe_quote(xt, period="tick")
        import time; time.sleep(0.3)
        tick = xtdata.get_full_tick([xt]).get(xt, {})
        return tick.get("lastPrice"), tick.get("highLimit")
    except Exception as e:
        print(f"  [warn] {code} xtdata取价失败: {e}")
        return None, None


def main():
    c = get_client()
    pos = c.get_positions()
    sh = SIG.get("shares", {}); pr = SIG.get("prices", {})
    print(f"\n{'='*78}\n6/22 补单修正  上浮{BUFFER:.0%} / 追高上限{MAX_CHASE:.0%} / 单笔≤¥{MAX_ORDER_AMT:,}"
          f"  [{'实盘下单' if GO else 'DRY-RUN'}]\n{'='*78}")
    print(f"{'代码':<8}{'缺口':>6}{'信号价':>8}{'实时价':>8}{'涨停价':>8}{'限价':>8}  处置")

    for code in TARGETS:
        held = pos.get(code, {}).get("volume", 0)
        gap = sh.get(code, 0) - held
        sig_p = pr.get(code, 0)
        if gap <= 0:
            print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{'-':>8}{'-':>8}{'-':>8}  跳过(已补齐)"); continue

        last, hi = live_quote(code)
        if last is None or last <= 0:
            print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{'?':>8}{'?':>8}{'?':>8}  跳过(无实时价,勿盲下)"); continue

        # 护栏1：涨停禁买
        if hi and last >= hi - 0.01:
            print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{last:>8.2f}{hi:>8.2f}{'-':>8}  跳过(涨停禁买)"); continue
        # 护栏2：追太远
        chase = last / sig_p - 1 if sig_p else 0
        if chase > MAX_CHASE:
            print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{last:>8.2f}{(hi or 0):>8.2f}{'-':>8}  跳过(已涨{chase:+.0%},追高)"); continue

        limit = round(last * (1 + BUFFER), 2)
        if hi:
            limit = min(limit, round(hi - 0.01, 2))   # 永不超涨停
        # 护栏3：单笔≤5万，超限削股数
        qty = gap
        if limit * qty > MAX_ORDER_AMT:
            qty = int(MAX_ORDER_AMT / limit / LOT) * LOT
        if qty < LOT:
            print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{last:>8.2f}{(hi or 0):>8.2f}{limit:>8.2f}  跳过(削后不足1手)"); continue

        note = f"买{qty}股@{limit}" + (f" (削自{gap})" if qty < gap else "")
        print(f"{code:<8}{gap:>6}{sig_p:>8.2f}{last:>8.2f}{(hi or 0):>8.2f}{limit:>8.2f}  {note}")
        if GO:
            oid = c.place_order(code, "buy", qty, limit, "limit")
            print(f"         → order_id={oid}")

    print(f"{'='*78}")
    if not GO:
        print("DRY-RUN：以上为计划。确认无误后加 --go 实际下单。\n")


if __name__ == "__main__":
    main()
