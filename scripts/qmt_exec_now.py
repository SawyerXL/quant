# QMT rebalance: sell 1 + buy 7 -> 30 stocks
import sys, os, json, time, requests
from pathlib import Path

ROOT = Path(r"H:\quant")
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")

def rt_price(code):
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
        return float(r.text.split('"')[1].split(',')[3])
    except:
        return None

from execution.qmt_client import get_client
c = get_client()

# Step 1: Cancel old pending orders
orders = c.get_today_orders()
pending = [o for o in orders if o.get('status') not in (56, 57)]
print(f"Cancelling {len(pending)} old orders...")
for o in pending:
    oid = o.get('order_id', 0)
    if oid:
        c.cancel_order(oid)
time.sleep(2)

# Step 2: Current positions
raw = c.get_positions()
held = {x.split(".")[0] for x, v in raw.items() if v.get("volume", 0) > 0}
print(f"Current: {len(held)} positions")

# Step 3: Load signal
sig = json.loads(Path("H:/quant/data_store/meta/signal_a_latest.json").read_text(encoding="utf-8"))
target = set(sig['holdings'])
shares_map = sig.get('shares', {})

sell_list = sorted(held - target)
buy_list = sorted(target - held)

print(f"Sell: {sell_list}")
print(f"Buy: {buy_list}")

# Step 4: SELL
for code in sell_list:
    for x, v in raw.items():
        if x.split(".")[0] == code:
            vol = v.get('volume', 0)
            rt = rt_price(code)
            if rt and vol > 0:
                oid = c.place_order(code, "sell", vol, rt * 0.995)
                print(f"SELL {code} {vol}股 @~{rt:.2f} oid={oid}")
            break
time.sleep(2)

# Step 5: BUY
for code in buy_list:
    shares = shares_map.get(code, 0)
    rt = rt_price(code)
    if rt and shares > 0:
        oid = c.place_order(code, "buy", shares, rt * 1.003)
        print(f"BUY {code} {shares}股 @~{rt:.2f} oid={oid}")
time.sleep(5)

# Step 6: Verify
raw = c.get_positions()
final = {x.split(".")[0]: v for x, v in raw.items() if v.get("volume", 0) > 0}
acc = c.get_account_info()
print(f"\nDONE: {len(final)} positions | MktVal: {acc.get('market_value', 0):,.0f}")
