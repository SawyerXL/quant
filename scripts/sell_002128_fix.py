import os,sys,time
os.environ["ENV"]="simulation"; sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client,_to_xt_code
CODE="002128"; SHARES=900   # 三止损全触发, 清仓
c=get_client()
pos=c.get_positions()
# 兼容带后缀键, 实际持仓核对
held=0
for k,v in pos.items():
    if k.split(".")[0]==CODE: held=v.get("volume",0)
print("002128 实际持仓=%s (拟卖%s)"%(held,SHARES))
SHARES=min(SHARES,held) if held>0 else SHARES
last=lowlim=0
try:
    from xtquant import xtdata
    xt=_to_xt_code(CODE); xtdata.subscribe_quote(xt,period="tick"); time.sleep(0.6)
    t=xtdata.get_full_tick([xt])[xt]; last=float(t["lastPrice"]); lowlim=float(t.get("lowLimit") or 0)
except Exception as e:
    print("price err",e)
print("002128 last=%s 跌停价=%s"%(last,lowlim))
if last<=0:
    print("无价,未下单")
elif lowlim>0 and last<=lowlim+0.01:
    print("跌停禁卖(红线), 未下单")
elif SHARES<100:
    print("无可卖持仓")
else:
    limit=round(last*0.995,2)
    oid=c.place_order(CODE,"sell",SHARES,limit,"limit")
    print("SELL 002128 %s@%s -> order_id=%s"%(SHARES,limit,oid))
