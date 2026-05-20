"""
用系统 Python 从外部连接 Matrix QMT 服务器测试
在 H:\quant 目录下运行：python scripts\matrix_strategies\test_ext_connect.py
"""
import sys

QMT_PATH = (
    r"H:\BaiduNetdiskDownload\申万宏源极速交易系统UAT环境接入资料"
    r"\Matrix仿真交易终端\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    r"\申万宏源策略量化交易终端-2.0.13版本\userdata_mini"
)
ACCOUNT_ID = "1633013579"

print("Python:", sys.version[:10])
print("QMT Path:", QMT_PATH)

try:
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    print("xtquant import OK")

    trader  = XtQuantTrader(QMT_PATH, 123456)
    account = StockAccount(ACCOUNT_ID)

    print("连接中...")
    result = trader.connect()
    print("连接结果:", result)

    if result == 0:
        print("连接成功！查询账户信息...")
        trader.start()
        asset = trader.query_stock_asset(account)
        print("账户资产:", asset)
        positions = trader.query_stock_positions(account)
        print("持仓:", positions)
    else:
        print("连接失败，确认 Matrix 终端是否正在运行且已登录")

except ImportError as e:
    print("xtquant 未安装:", e)
    print("请先运行: pip install xtquant")
except Exception as e:
    print("错误:", type(e).__name__, str(e))
