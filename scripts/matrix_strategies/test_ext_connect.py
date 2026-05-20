"""用系统 Python 从外部连接 Matrix QMT 服务器测试"""
import sys, time

BASE = (
    "H:\\BaiduNetdiskDownload\\申万宏源极速交易系统UAT环境接入资料"
    "\\Matrix仿真交易终端\\申万宏源QMT仿真环境策略量化交易终端-2.0.13"
    "\\申万宏源策略量化交易终端-2.0.13版本"
)
ACCOUNT_ID = "1633013579"

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

# 试两个路径
for sub in ["userdata_mini", "userdata"]:
    path = BASE + "\\" + sub
    print(f"\n=== 测试路径: {sub} ===")
    try:
        trader = XtQuantTrader(path, 123456)
        # 先 start，再 connect
        trader.start()
        time.sleep(2)
        result = trader.connect()
        print("connect() 返回:", result)

        if result == 0:
            print("连接成功！")
            account = StockAccount(ACCOUNT_ID)
            asset = trader.query_stock_asset(account)
            print("资产:", asset)
            break
        else:
            print(f"路径 {sub} 连接失败 (result={result})")
            trader.stop()
    except Exception as e:
        print("异常:", type(e).__name__, e)

print("\n如果两个路径都失败：")
print("1. 确认 Matrix 终端正在运行并已登录账号", ACCOUNT_ID)
print("2. 确认 Matrix 终端界面没有弹出错误对话框")
print("3. 查看 Matrix 终端的日志或状态栏")
