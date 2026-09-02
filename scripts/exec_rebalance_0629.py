"""6/29 QMT满仓调仓 — 成交额TOP30等权 (防重复执行版 + 风控接入版, 2026-09-02)

2026-09-02 修复(审查P0-3):
  ① 未成交挂单守卫原用不存在的get_pending_orders → 恒空死代码;
    改用get_today_orders, 按 filled<shares 判未成交。
  ② 执行锁原在下单后才写 → 中途崩溃重跑=双份买单; 改为先占锁再下单。
  ③ 原全部订单绕过RiskGateway直连place_order → 违反"所有订单过
    execution/risk.py"红线; 改为每笔过风控(分笔≤10万, 涨跌停/熔断/
    单票/行业全查)。
注: 此脚本为一次性手动满仓调仓工具, 日常调仓请走 fetch_and_execute.py。
"""
import os, json, sys, datetime
os.environ["ENV"] = "production"
sys.path.insert(0, "H:/quant")
from execution.qmt_client import get_client
from execution.trader import Trader
from execution.risk import RiskGateway

# ═══ 防重复执行 ═══
LOCK_FILE = "H:/quant/logs/rebalance_lock.json"
today = datetime.date.today().strftime("%Y-%m-%d")

if os.path.exists(LOCK_FILE):
    try:
        lock = json.loads(open(LOCK_FILE, "r", encoding="utf-8").read())
        if lock.get("date") == today:
            print(f"[SKIP] 今日已执行过调仓 ({lock.get('date')})，退出。")
            sys.exit(0)
    except Exception:
        pass  # lock file corrupted, proceed

c = get_client()

# ═══ 检查是否有未成交挂单(2026-09-02 修复: 用真实存在的接口) ═══
try:
    open_orders = [o for o in c.get_today_orders()
                   if o.get("filled", 0) < o.get("shares", 0)]
except Exception:
    open_orders = []
if open_orders:
    pending_count = sum(o["shares"] - o.get("filled", 0) for o in open_orders)
    print(f"[SKIP] 有 {pending_count} 股未成交挂单，先处理完再调仓。")
    sys.exit(0)

# ═══ 先占执行锁再下单(2026-09-02 修复: 原下单后才写锁, 崩溃重跑=重复下单) ═══
os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
json.dump({
    "date": today,
    "time": datetime.datetime.now().strftime("%H:%M:%S"),
    "state": "started",
}, open(LOCK_FILE, "w", encoding="utf-8"))

# ═══ 读取信号 ═══
sig = json.loads(open("H:/quant/data_store/meta/signal_a_latest.json", "r", encoding="utf-8").read())
target_shares = sig["shares"]
target_holdings = set(sig["holdings"])
ref_prices = sig.get("prices", {})

pos = c.get_positions()
current = {}
for k, v in pos.items():
    code = k.split('.')[0] if '.' in k else k
    current[code] = v["volume"]
print(f"QMT当前持仓: {len(current)}只")

# ═══ 风控网关(2026-09-02: 与日常执行同口径, 100万名义资金) ═══
t = Trader()
gw, _ = t._build_gateway("track_a", 1_000_000)
from execution.trader import _limit_prices, _build_st_map
check_codes = sorted(set(current) | set(target_holdings))
prices_all = dict(ref_prices)
for code in check_codes:
    if code not in prices_all:
        p = [v for k, v in pos.items() if k.split('.')[0] == code]
        if p and p[0].get("cost_price", 0) > 0:
            prices_all[code] = float(p[0]["cost_price"])
gw.state["limit_prices"] = _limit_prices(check_codes, prices_all, _build_st_map())

MAX_ORD = 100_000

def _checked_order(code, direction, vol, price):
    """分笔≤10万 + 全量风控检查后下单。"""
    remaining = vol
    while remaining > 0:
        q = min(remaining, int(MAX_ORD / max(price, 0.01)))
        if q <= 0:
            q = remaining
        ok, reason = gw.check("track_a", code, direction, q, price)
        if not ok:
            print(f"[风控拦截] {direction} {code} {q}股: {reason}")
            return
        c.place_order(code, direction, q, 0, "market")
        remaining -= q

# ═══ 卖出不在目标池的 ═══
sell_list = [c for c in current if c not in target_holdings]
print(f"\n=== SELL {len(sell_list)}只 ===")
for code in sell_list:
    vol = current[code]
    if vol <= 0:
        continue
    _checked_order(code, "sell", vol, prices_all.get(code, 1.0))

# ═══ 买入信号里不在当前持仓的 ═══
buy_list = [(c, target_shares[c]) for c in target_holdings if c not in current]
print(f"\n=== BUY {len(buy_list)}只 ===")
for code, vol in buy_list:
    if vol <= 0:
        continue
    _checked_order(code, "buy", vol, prices_all.get(code, 1.0))

# ═══ 更新执行锁 ═══
after = len(current) - len(sell_list) + len(buy_list)
json.dump({
    "date": today,
    "time": datetime.datetime.now().strftime("%H:%M:%S"),
    "state": "done",
    "sell_count": len(sell_list),
    "buy_count": len(buy_list),
}, open(LOCK_FILE, "w", encoding="utf-8"))
print(f"\n=== DONE: {after}只 (目标{len(target_holdings)}) ===")
