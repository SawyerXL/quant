"""单笔下单测试 - 诊断用"""
import sys, time
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
import xtquant.xtconstant as xtc

QMT_PATH = (
    "H:\\BaiduNetdiskDownload\\申万宏源极速交易系统UAT环境接入资料"
    "\\Matrix仿真交易终端\\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    "\\申万宏源策略量化交易终端-2.0.13版本\\userdata_mini"
)

# 每次用不同session_id
session_id = int(time.time())
print(f"session_id: {session_id}")

trader = XtQuantTrader(QMT_PATH, session_id)
account = StockAccount("1633013579")
trader.start()
time.sleep(3)
r = trader.connect()
print(f"连接: {'成功' if r==0 else f'失败({r})'}")
if r != 0:
    sys.exit(1)
time.sleep(1)

# 测试下单：000426 兴业银行，100股，限价
code   = "000426.SZ"
shares = 100
price  = 42.0   # 略高于市价

print(f"尝试买入 {code} {shares}股 @{price}")
oid = trader.order_stock(
    account, code,
    xtc.STOCK_BUY, shares,
    xtc.FIX_PRICE, price,
    strategy_name="test",
    order_remark=f"test_{session_id}"
)
print(f"order_id: {oid}")

if oid and oid > 0:
    print("✅ 下单成功！order_id 有效")
else:
    print("❌ 下单失败，order_id 无效")

trader.stop()
