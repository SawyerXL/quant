"""6/29 QMT满仓调仓 — 成交额TOP30等权"""
import os, json, sys
os.environ["ENV"] = "production"
sys.path.insert(0, "H:/quant")
from execution.qmt_client import get_client

c = get_client()
sig = json.loads(open("H:/quant/data_store/meta/signal_a_latest.json", "r", encoding="utf-8").read())
shares = sig["shares"]
holdings = set(sig["holdings"])

pos = c.get_positions()
# QMT returns codes like '002008.SZ' — strip exchange suffix
current = {}
for k, v in pos.items():
    code = k.split('.')[0] if '.' in k else k
    current[code] = v["volume"]
print(f"QMT当前持仓: {len(current)}只")

sell_list = [c for c in current if c not in holdings]
print(f"\n=== SELL {len(sell_list)}只 ===")
for code in sell_list:
    vol = current[code]
    if vol <= 0: continue
    o = c.place_order(code, "sell", vol, 0, "market")
    print(f"SELL {code} {vol}股 -> {o}")

buy_list = [(c, shares[c]) for c in holdings if c not in current]
print(f"\n=== BUY {len(buy_list)}只 ===")
for code, vol in buy_list:
    if vol <= 0: continue
    o = c.place_order(code, "buy", vol, 0, "market")
    print(f"BUY {code} {vol}股 -> {o}")

after = len(current) - len(sell_list) + len(buy_list)
print(f"\n=== DONE: {after}只 (目标30) ===")
