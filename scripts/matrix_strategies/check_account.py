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
time.sleep(1)
result = trader.connect()
print("连接:", "成功" if result == 0 else f"失败({result})")

if result == 0:
    trader.start()
    time.sleep(1)

    # 账户资产
    asset = trader.query_stock_asset(account)
    if asset:
        print("\n=== 账户资产 ===")
        print(f"  总资产:  {asset.total_asset:,.2f} 元")
        print(f"  可用现金: {asset.cash:,.2f} 元")
        print(f"  持仓市值: {asset.market_value:,.2f} 元")
        print(f"  冻结资金: {asset.frozen_cash:,.2f} 元")
    else:
        print("资产查询返回空")

    # 持仓
    positions = trader.query_stock_positions(account)
    if positions:
        print(f"\n=== 持仓（{len(positions)} 只）===")
        for p in positions[:5]:
            print(f"  {p.stock_code}: {p.volume}股，成本{p.open_price:.2f}，市值{p.market_value:.2f}")
    else:
        print("\n持仓：空仓（正常，还未建仓）")

trader.stop()
print("\n测试完成")
