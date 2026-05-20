"""查询 QMT 仿真账户余额和持仓"""
import sys, time

QMT_PATH = (
    "H:\\BaiduNetdiskDownload\\申万宏源极速交易系统UAT环境接入资料"
    "\\Matrix仿真交易终端\\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    "\\申万宏源策略量化交易终端-2.0.13版本\\userdata_mini"
)
ACCOUNT_ID = "1633013579"

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

trader  = XtQuantTrader(QMT_PATH, 123456)
account = StockAccount(ACCOUNT_ID)

trader.start()
time.sleep(2)
result = trader.connect()
print("连接:", "成功" if result == 0 else f"失败({result})")

if result != 0:
    sys.exit(1)

# 订阅账户后再查询（部分版本需要先订阅）
time.sleep(2)
print("查询账户资产...")
asset = trader.query_stock_asset(account)
print("asset 类型:", type(asset))
print("asset 值:", asset)

if asset:
    try:
        print(f"  总资产:   {asset.total_asset:,.2f} 元")
        print(f"  可用现金: {asset.cash:,.2f} 元")
        print(f"  持仓市值: {asset.market_value:,.2f} 元")
    except Exception as e:
        print("  属性读取错误:", e)
        print("  asset 属性:", dir(asset))

print("\n查询持仓...")
positions = trader.query_stock_positions(account)
print("positions 类型:", type(positions))
print("positions 值:", positions)

trader.stop()
print("完成")
