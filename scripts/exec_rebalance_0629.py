"""
6/29 QMT满仓调仓 — 成交额TOP30等权
powershell: cd H:\quant && python scripts\exec_rebalance_0629.py
"""
import os, json, sys
os.environ["ENV"] = "live"
sys.path.insert(0, "H:/quant")
from execution.qmt_client import get_client

c = get_client()
sig = json.loads(open("H:/quant/data_store/meta/signal_a_latest.json", "r", encoding="utf-8").read())
shares = sig["shares"]
prices = sig["prices"]
holdings = set(sig["holdings"])

# ═══ 获取QMT当前持仓 ═══
pos = c.get_positions()
current = {p["code"]: p["volume"] for p in pos if p["volume"] > 0}
print(f"当前持仓: {len(current)}只")

# ═══ SELL: 不在信号池的 ═══
sell_list = [c for c in current if c not in holdings]
print(f"\n=== SELL ({len(sell_list)}只) ===")
for code in sell_list:
    vol = current[code]
    o = c.place_order(code, "sell", vol, 0, "market")  # 市价卖出
    print(f"SELL {code} {vol}股 -> {o}")

# ═══ BUY: 信号池内未持有的 ═══
buy_list = [(c, shares[c]) for c in holdings if c not in current]
print(f"\n=== BUY ({len(buy_list)}只) ===")
for code, vol in buy_list:
    if vol <= 0: continue
    o = c.place_order(code, "buy", vol, 0, "market")
    print(f"BUY {code} {vol}股 -> {o}")

print(f"\n=== DONE ===")
print(f"卖出: {len(sell_list)}只, 买入: {len(buy_list)}只")
print(f"调仓后: {len(current) - len(sell_list) + len(buy_list)}只 (目标30)")
