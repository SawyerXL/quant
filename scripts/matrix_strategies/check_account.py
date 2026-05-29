"""查询 QMT 仿真账户余额和持仓详情"""
import sys, time
sys.path.insert(0, '.')

QMT_PATH = (
    "H:\\BaiduNetdiskDownload\\申万宏源极速交易系统UAT环境接入资料"
    "\\Matrix仿真交易终端\\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    "\\申万宏源策略量化交易终端-2.0.13版本\\userdata_mini"
)
ACCOUNT_ID = "1633013579"

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

trader  = XtQuantTrader(QMT_PATH, int(time.time()) % 100000)
account = StockAccount(ACCOUNT_ID)
trader.start()
time.sleep(2)
result = trader.connect()
print("连接:", "成功" if result == 0 else f"失败({result})")

if result == 0:
    asset = trader.query_stock_asset(account)
    if asset:
        print(f"\n账户资产:")
        print(f"  总资产:   {asset.total_asset:>15,.2f} 元")
        print(f"  可用现金: {asset.cash:>15,.2f} 元")
        print(f"  持仓市值: {asset.market_value:>15,.2f} 元")

    positions = trader.query_stock_positions(account)
    if positions:
        print(f"\n持仓明细（共 {len(positions)} 只）:")
        print(f"  {'代码':<10} {'股数':>8} {'成本价':>10} {'市值':>12}")
        print(f"  {'-'*44}")
        total_mv = 0
        for p in sorted(positions, key=lambda x: -x.market_value):
            print(f"  {p.stock_code:<10} {p.volume:>8,} {p.open_price:>10.2f} {p.market_value:>12,.2f}")
            total_mv += p.market_value
        print(f"  {'-'*44}")
        print(f"  {'合计':<10} {'':>8} {'':>10} {total_mv:>12,.2f}")
    else:
        print("\n持仓: 空仓")

trader.stop()
