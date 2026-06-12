import os;os.environ["ENV"]="simulation"
import sys;sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client
c=get_client()
print("QMT已连接")

# 卖出
print(c.place_order("002155","sell",100,-1,"market"))
print(c.place_order("600578","sell",2100,-1,"market"))
print(c.place_order("600816","sell",4800,-1,"market"))

# 买入
print(c.place_order("001965","buy",3300,9.76,"limit"))
print(c.place_order("002008","buy",200,121.45,"limit"))
print(c.place_order("002025","buy",300,66.06,"limit"))
print(c.place_order("002085","buy",2500,12.35,"limit"))
print(c.place_order("002142","buy",300,32.85,"limit"))
print(c.place_order("002920","buy",100,85.48,"limit"))
print(c.place_order("600295","buy",400,13.60,"limit"))
print(c.place_order("600378","buy",300,63.50,"limit"))
print(c.place_order("600901","buy",500,6.58,"limit"))
print(c.place_order("600909","buy",3300,7.27,"limit"))
print(c.place_order("600999","buy",800,17.24,"limit"))
print(c.place_order("601838","buy",200,19.76,"limit"))
print(c.place_order("601872","buy",1300,14.42,"limit"))
print(c.place_order("603156","buy",700,40.80,"limit"))
print(c.place_order("603688","buy",100,76.15,"limit"))