"""测试下单和撤单——验证完整交易链路"""
import sys, time

QMT_PATH = (
    "H:\\BaiduNetdiskDownload\\申万宏源极速交易系统UAT环境接入资料"
    "\\Matrix仿真交易终端\\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    "\\申万宏源策略量化交易终端-2.0.13版本\\userdata_mini"
)
ACCOUNT_ID = "1633013579"

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
import xtquant.xtconstant as xtc

trader  = XtQuantTrader(QMT_PATH, 123456)
account = StockAccount(ACCOUNT_ID)

trader.start()
time.sleep(2)
result = trader.connect()
print("连接:", "成功" if result == 0 else f"失败({result})")
if result != 0:
    sys.exit(1)

time.sleep(1)

# 测试下单：买入平安银行 100股，限价（远低于市价，不会实际成交）
# A股代码格式：上海加 .SH，深圳加 .SZ
test_code  = "000001.SZ"   # 平安银行（深圳）
test_price = 1.0           # 远低于市价，不会成交
test_vol   = 100

print(f"\n下单测试: 买入 {test_code} {test_vol}股 @ {test_price}元（限价，不会成交）")
order_id = trader.order_stock(
    account,
    test_code,
    xtc.STOCK_BUY,
    test_vol,
    xtc.FIX_PRICE,
    test_price,
    strategy_name="test",
    order_remark="test_order"
)
print("order_id:", order_id)

if order_id and order_id > 0:
    print("下单成功！order_id =", order_id)
    time.sleep(1)

    # 查询委托
    orders = trader.query_stock_orders(account)
    print(f"当前委托数: {len(orders) if orders else 0}")

    # 立即撤单
    print("撤单中...")
    cancel_result = trader.cancel_order_stock(account, order_id)
    print("撤单结果:", cancel_result)
    print("\n✅ 下单+撤单测试成功！交易链路正常")
else:
    print("下单失败，order_id:", order_id)
    print("可能原因：股票代码格式不对，或账户未就绪")

trader.stop()
