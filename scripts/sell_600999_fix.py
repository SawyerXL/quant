import os,sys,time
os.environ["ENV"]="simulation"; sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client,_to_xt_code
CODE="600999"; SHARES=800
c=get_client()
last=0
try:
    from xtquant import xtdata
    xt=_to_xt_code(CODE); xtdata.subscribe_quote(xt,period="tick"); time.sleep(0.6)
    last=float(xtdata.get_full_tick([xt])[xt]["lastPrice"])
except Exception as e:
    print("price err",e)
limit=round(last*0.995,2) if last>0 else 0
print("600999 last=%s sell_limit=%s shares=%s"%(last,limit,SHARES))
if limit>0:
    oid=c.place_order(CODE,"sell",SHARES,limit,"limit")
    print("SELL 600999 %s@%s -> order_id=%s"%(SHARES,limit,oid))
else:
    print("no price, not placed")
