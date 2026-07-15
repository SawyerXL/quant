"""
应急调仓 — 对齐到信号目标(卖多余+补缺失), 收盘前速执行
"""
import os, sys, json, time
os.environ["ENV"] = "simulation"
sys.path.insert(0, "H:/quant")
from execution.qmt_client import get_client

c = get_client()
sig = json.loads(open("H:/quant/data_store/meta/signal_a_latest.json", "r", encoding="utf-8").read())
target = sig.get("shares", {})
prices = sig.get("prices", {})
pos = c.get_positions()
actual = {c.split(".")[0]: v.get("volume",0) if isinstance(v,dict) else getattr(v,"volume",0) 
          for c,v in pos.items() if (isinstance(v,dict) and v.get("volume",0)>0) or (hasattr(v,"volume") and v.volume>0)}
print(f"实盘{len(actual)}只 信号{len(target)}只")

# Sell: 实盘有/信号不要 或 实盘>目标
sells = 0
for code in list(actual.keys()):
    t = target.get(code, 0)
    if t <= 0:
        limit = round((prices.get(code, 0) or 1) * 0.98, 2)
        try:
            oid = c.place_order(code, "sell", actual[code], limit, "limit")
            print(f"SELL {code} {actual[code]}股 @{limit} -> {oid}")
            sells += 1; time.sleep(0.3)
        except Exception as e: print(f"SELL {code} FAIL: {e}")

# Buy: 缺的
time.sleep(3)
buys = 0
for code, t in target.items():
    if t <= 0: continue
    need = t - actual.get(code, 0)
    if need >= 100:
        limit = round((prices.get(code, 0) or 1) * 1.02, 2)
        try:
            oid = c.place_order(code, "buy", need, limit, "limit")
            print(f"BUY {code} {need}股 @{limit} -> {oid}")
            buys += 1; time.sleep(0.3)
        except Exception as e: print(f"BUY {code} FAIL: {e}")

print(f"\nDone: SELL {sells} BUY {buys}")
