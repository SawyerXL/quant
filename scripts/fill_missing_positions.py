"""
5/29 补仓脚本：只买入信号里 QMT 尚未持有的股票，已持有的不重复买。
"""
import sys, time, json
sys.path.insert(0, '.')
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from execution.qmt_client import get_client, _to_xt_code
import xtquant.xtconstant as xtc

# 读取最新信号
sig = json.loads(Path("data_store/meta/signal_a_latest.json").read_text(encoding="utf-8"))
target_holdings = set(sig["holdings"])
signal_prices   = sig.get("prices", {})
signal_shares   = sig.get("shares", {})

print(f"信号日期: {sig['signal_date']}")
print(f"目标持仓: {len(target_holdings)} 只")

# 连接 QMT，查询当前持仓
c = get_client()
time.sleep(1)

positions = c.trader.query_stock_positions(c.account)
held_codes = set()
if positions:
    for p in positions:
        code = p.stock_code.replace(".SZ","").replace(".SH","").replace(".BJ","")
        held_codes.add(code)

print(f"QMT当前持仓: {len(held_codes)} 只 → {sorted(held_codes)}")

# 找出需要补买的
missing = sorted(target_holdings - held_codes)
print(f"\n需要补买: {len(missing)} 只")

skip_count = 0
orders = []
for code in missing:
    s = signal_shares.get(code, 0)
    p = signal_prices.get(code, 0)
    if s <= 0 or p <= 0:
        print(f"  跳过 {code}: 股数={s} 价格={p}（价格过高买不到一手）")
        skip_count += 1
        continue
    orders.append((code, s, p))

print(f"实际下单: {len(orders)} 只，跳过: {skip_count} 只")
print()

for code, shares, price in orders:
    limit_price = round(price * 1.10, 2)
    oid = c.trader.order_stock(
        c.account, _to_xt_code(code),
        xtc.STOCK_BUY, shares, xtc.FIX_PRICE, limit_price,
        strategy_name='quant', order_remark='fill_missing'
    )
    print(f"  买入 {code} {shares}股 @{limit_price} (信号价{price}) → order_id={oid}")
    time.sleep(0.1)

print(f"\n完成！去 Matrix 查看委托（应有 {len(orders)} 笔）")
