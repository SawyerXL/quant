"""
实盘止损监控器 (跑在 Windows QMT 机, 自包含: xtdata取价 + QMT执行)。
补上"信号生成了却无人执行"的窟窿。

v2 (2026-07-09): 停用追踪止损, 只留成本硬止损。
  - 期内止损: 现价/成本 - 1 <= -15%  (保留: 单票崩盘/黑天鹅兜底)
  - 追踪止损: 已停用 (仍计算并显示 tdd 供参考, 但不再据此卖出)
  变更依据 (backtest_stoploss_ab.py, CSI800 2019-2026 逐股级A/B):
    追踪-18%对组合最大回撤零贡献(各变种回撤都≈-20%), 却把7.2%年化拖到最差,
    全周期35次在成本之上砍掉浮盈票(如7/9的300394 +4.8%被割)。
    去掉追踪后年化7.2%→7.9%、夏普0.45→0.49、切浮盈35→0, 回撤不变。
红线: 跌停禁卖; 默认 DRY-RUN, 加 --go 才真卖。
计划: Windows 计划任务, 盘中每 ~20 分钟跑一次(带 --go)。
用法: python scripts/stop_monitor.py        # 空跑
      python scripts/stop_monitor.py --go   # 实盘执行
"""
import os,sys,json,time
os.environ["ENV"]="simulation"; sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client,_to_xt_code

PERIOD_STOP=-0.15; TRAIL_STOP=None   # TRAIL_STOP=None → 追踪止损停用(见docstring A/B依据)
STATE=os.path.join("H:/quant/logs","stop_state.json")
GO="--go" in sys.argv

def load_state():
    try: return json.load(open(STATE,encoding="utf-8"))
    except Exception: return {}
def save_state(s):
    try: json.dump(s,open(STATE,"w",encoding="utf-8"))
    except Exception as e: print("state save err",e)

def live(code):
    try:
        from xtquant import xtdata
        xt=_to_xt_code(code); xtdata.subscribe_quote(xt,period="tick"); time.sleep(0.4)
        t=xtdata.get_full_tick([xt]).get(xt,{})
        return float(t.get("lastPrice") or 0), float(t.get("lowLimit") or 0)
    except Exception:
        return 0.0,0.0

def main():
    c=get_client(); pos=c.get_positions(); st=load_state()
    print("="*70); print("止损监控 [%s]  期内%.0f%% / 追踪%s"%("实盘" if GO else "DRY-RUN",PERIOD_STOP*100,"停用" if TRAIL_STOP is None else "%.0f%%"%(TRAIL_STOP*100))); print("="*70)
    print("%-8s%8s%8s%8s%9s%9s  处置"%("代码","成本","现价","峰值","距成本","距峰"))
    sells=[]
    for k,v in pos.items():
        code=k.split(".")[0]; vol=v.get("volume",0); cost=v.get("cost_price",0)
        if vol<=0 or cost<=0: continue
        last,lowlim=live(code)
        if last<=0:
            print("%-8s%8.2f%8s 无实时价,跳过"%(code,cost,"?")); continue
        peak=max(st.get(code,{}).get("peak",cost),last,cost); st[code]={"peak":peak}
        pdd=last/cost-1; tdd=last/peak-1
        hit = pdd<=PERIOD_STOP or (TRAIL_STOP is not None and tdd<=TRAIL_STOP)
        note=""
        if hit:
            if lowlim>0 and last<=lowlim+0.01: note="触发止损但跌停禁卖(红线)"
            else:
                note="止损卖出 %s股@%s"%(vol,round(last*0.995,2))
                sells.append((code,vol,round(last*0.995,2)))
        print("%-8s%8.2f%8.2f%8.2f%8.1f%%%8.1f%%  %s"%(code,cost,last,peak,pdd*100,tdd*100,note or "持有"))
    save_state(st)
    print("="*70)
    if GO and sells:
        from monitoring.alerts import send_alert
        for code,vol,limit in sells:
            oid=c.place_order(code,"sell",vol,limit,"limit")
            print("SELL %s %s@%s -> %s"%(code,vol,limit,oid))
        try: send_alert("[止损执行] "+", ".join("%s %s股@%s"%(s[0],s[1],s[2]) for s in sells))
        except Exception: pass
    elif sells:
        print("DRY-RUN: 以上%d只触发止损, 加 --go 执行"%len(sells))
    else:
        print("无触发止损")

if __name__=="__main__": main()
