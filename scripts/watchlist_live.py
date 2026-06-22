import os,sys,time,json
os.environ["ENV"]="simulation"; sys.path.insert(0,"H:/quant")
CODES=["601899","603392","600893","300124","002559","000538","300408"]
def xt(c): return c+(".SH" if c[0]=="6" else (".BJ" if c[0] in "489" else ".SZ"))
try:
    from xtquant import xtdata
    xs=[xt(c) for c in CODES]
    for x in xs: xtdata.subscribe_quote(x,period="tick")
    time.sleep(1.0)
    t=xtdata.get_full_tick(xs)
    out={c: float(t.get(xt(c),{}).get("lastPrice") or 0) for c in CODES}
    print("LIVE="+json.dumps(out))
except Exception as e:
    print("ERR",e)
