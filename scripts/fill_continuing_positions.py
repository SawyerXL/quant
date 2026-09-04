"""补买调仓后的续仓股票（QMT账户缺失的持仓）"""
import sys, time
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from execution.qmt_client import get_client, _to_xt_code
import xtquant.xtconstant as xtc

c = get_client()

orders = [
    ('002085', 3400, 12.39),
    ('300058',  400, 15.77),
    ('601872',  200, 16.29),
    ('601991', 2200,  7.71),
    ('603156',  600, 43.97),
]

print("补买续仓股票...")
for code, shares, price in orders:
    # 2026-09-04 修复: 原直连order_stock绕过tick归一(CLAUDE.md红线:
    # 所有下单必须经qmt_client); place_order自动tick归一
    oid = c.place_order(code, "buy", shares, price * 1.10)
    print(f"  {code} {shares}股 @{price * 1.10:.2f} → order_id={oid}")
    time.sleep(0.1)

print("完成！去 Matrix 查看委托。")
