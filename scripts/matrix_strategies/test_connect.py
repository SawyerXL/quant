import os
import sys
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

base = os.path.dirname(sys.path[0])
path = os.path.join(base, "userdata_mini")
print("userdata_mini:", path)

trader = XtQuantTrader(path, 123456)
account = StockAccount("1633013579")
result = trader.connect()
print("连接结果:", result)

positions = trader.query_stock_positions(account)
print("持仓:", positions)
