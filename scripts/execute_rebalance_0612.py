"""
6/12 调仓日执行脚本 — 在Windows QMT服务器上运行
用法: python scripts/execute_rebalance_0612.py
"""
import os;os.environ["ENV"]="simulation"
import sys;sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client

c=get_client()
acc=c.get_account_info()
print(f"QMT connected, cash: {acc['cash']:,.0f}")

# === SELL ===
oid=c.place_order("002155","sell",100,23.21,"limit")
print(f"SELL 002155 100股 @23.21 -> {oid}")
oid=c.place_order("600578","sell",2100,7.74,"limit")
print(f"SELL 600578 2100股 @7.74 -> {oid}")
oid=c.place_order("600816","sell",4800,2.6,"limit")
print(f"SELL 600816 4800股 @2.6 -> {oid}")

# === BUY ===
oid=c.place_order("001965","buy",3300,10.05,"limit")
print(f"BUY  001965 3300股 @10.05 -> {oid}")
oid=c.place_order("002008","buy",200,125.09,"limit")
print(f"BUY  002008 200股 @125.09 -> {oid}")
oid=c.place_order("002025","buy",300,68.04,"limit")
print(f"BUY  002025 300股 @68.04 -> {oid}")
oid=c.place_order("002085","buy",2500,12.72,"limit")
print(f"BUY  002085 2500股 @12.72 -> {oid}")
oid=c.place_order("002142","buy",300,33.84,"limit")
print(f"BUY  002142 300股 @33.84 -> {oid}")
oid=c.place_order("002920","buy",100,88.04,"limit")
print(f"BUY  002920 100股 @88.04 -> {oid}")
oid=c.place_order("600295","buy",400,14.01,"limit")
print(f"BUY  600295 400股 @14.01 -> {oid}")
oid=c.place_order("600378","buy",300,65.41,"limit")
print(f"BUY  600378 300股 @65.41 -> {oid}")
oid=c.place_order("600901","buy",500,6.78,"limit")
print(f"BUY  600901 500股 @6.78 -> {oid}")
oid=c.place_order("600909","buy",3300,7.49,"limit")
print(f"BUY  600909 3300股 @7.49 -> {oid}")
oid=c.place_order("600999","buy",800,17.76,"limit")
print(f"BUY  600999 800股 @17.76 -> {oid}")
oid=c.place_order("601838","buy",200,20.35,"limit")
print(f"BUY  601838 200股 @20.35 -> {oid}")
oid=c.place_order("601872","buy",1300,14.85,"limit")
print(f"BUY  601872 1300股 @14.85 -> {oid}")
oid=c.place_order("603156","buy",700,42.02,"limit")
print(f"BUY  603156 700股 @42.02 -> {oid}")
oid=c.place_order("603688","buy",100,78.43,"limit")
print(f"BUY  603688 100股 @78.43 -> {oid}")
print("DONE - all orders submitted")