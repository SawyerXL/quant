"""
实盘止损+止盈监控器 (跑在 Windows QMT 机, 自包含: xtdata取价 + QMT执行)。

v4 (2026-08-12): TP2从+60%改为+45%全清 + 回撤-8%兜底 (backtest_tp_exit.py回测最优)
  - 止损: 成本-15%全卖
  - 追踪止损: 已停用
  - 止盈: TP1 +30%卖1/3, TP2 +45%剩余全清
  - 兜底: TP1后从最高点回落8%即全清(防利润归零)
  - TP状态持久化在 stop_state.json (tp_level + tp_peak)

v3 (2026-07-20): 加固定分批止盈 +30%/60%。

v2 (2026-07-09): 停用追踪止损, 只留成本硬止损。

红线: 跌停禁卖; 默认 DRY-RUN, 加 --go 才真卖。
计划: Windows 计划任务, 盘中每 ~20 分钟跑一次(带 --go)。
用法: python scripts/stop_monitor.py        # 空跑
      python scripts/stop_monitor.py --go   # 实盘执行
"""
import os,sys,json,time
os.environ["ENV"]="simulation"; sys.path.insert(0,"H:/quant")
from execution.qmt_client import get_client,_to_xt_code

PERIOD_STOP=-0.15; TRAIL_STOP=None   # TRAIL_STOP=None → 追踪止损停用(见docstring A/B依据)
# 止盈 v4: TP1 +30%卖1/3, TP2 +45%清仓 + 回撤-8%兜底 (backtest_tp_exit.py回测最优)
TAKE_PROFIT_1 = 0.30  # 第一档止盈(卖当前仓位1/3)
TAKE_PROFIT_2 = 0.45  # 第二档(剩余全清, 原0.60→0.45, +0.3pp年化)
TP_TRAIL      = 0.08  # TP1后兜底: 从TP1后最高点回落8%即全清(防利润归零)
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
    print("="*70); print("V2止损+止盈v4 [%s]  止损%.0f%% / TP1+%.0f%%卖1/3 / TP2+%.0f%%全清/兜底-%.0f%%"%("实盘" if GO else "DRY-RUN",PERIOD_STOP*100,TAKE_PROFIT_1*100,TAKE_PROFIT_2*100,TP_TRAIL*100)); print("="*70)
    print("%-8s%8s%8s%8s%9s%9s  处置"%("代码","成本","现价","峰值","距成本","距峰"))
    sells=[]
    for k,v in pos.items():
        code=k.split(".")[0]; vol=v.get("volume",0); cost=v.get("cost_price",0)
        if vol<=0 or cost<=0: continue
        last,lowlim=live(code)
        if last<=0:
            print("%-8s%8.2f%8s 无实时价,跳过"%(code,cost,"?")); continue
        peak=max(st.get(code,{}).get("peak",cost),last,cost)
        tp_level = st.get(code,{}).get("tp_level", 0)  # 0=未触发, 1=TP1已做, 2=TP2已做
        tp_peak  = st.get(code,{}).get("tp_peak", cost)  # TP1触发后的最高价(回撤兜底用)
        st[code]={"peak":peak, "tp_level":tp_level, "tp_peak":tp_peak}
        pdd=last/cost-1; tdd=last/peak-1
        ret = last/cost - 1  # 距成本收益率
        hit = pdd<=PERIOD_STOP or (TRAIL_STOP is not None and tdd<=TRAIL_STOP)
        note=""
        # V2止损(优先级最高)
        if hit:
            if lowlim>0 and last<=lowlim+0.01: note="触发止损但跌停禁卖(红线)"
            else:
                note="止损卖出 %s股@%s"%(vol,round(last*0.995,2))
                sells.append((code,vol,round(last*0.995,2)))
                st[code]["tp_level"] = 0; st[code]["tp_peak"] = cost  # 止损出场, 重置TP
        # ── TP1: +30%卖1/3 ──
        elif ret >= TAKE_PROFIT_1 and tp_level < 1:
            sell_vol = max(100, int(vol / 3 / 100) * 100)
            note = "止盈TP1(+%.0f%%) 卖%s股@%s"%(ret*100, sell_vol, round(last,2))
            sells.append((code, sell_vol, round(last*0.995,2)))
            st[code]["tp_level"] = 1; st[code]["tp_peak"] = last  # 记录TP1触发时的价格作为起点
        # ── TP1后: TP2(+45%)或回撤兜底(-8%从tp_peak) ──
        elif tp_level == 1:
            tp_peak = max(tp_peak, last); st[code]["tp_peak"] = tp_peak  # 更新TP后峰值
            if ret >= TAKE_PROFIT_2:
                note = "止盈TP2(+%.0f%%) 全卖%s股@%s"%(ret*100, vol, round(last,2))
                sells.append((code, vol, round(last*0.995,2)))
                st[code]["tp_level"] = 2
            elif (tp_peak - last) / cost >= TP_TRAIL:  # 从TP1后峰值回落8%
                note = "止盈兜底(峰%.2f回撤%.0f%%) 全卖%s股@%s"%(tp_peak, (tp_peak-last)/cost*100, vol, round(last,2))
                sells.append((code, vol, round(last*0.995,2)))
                st[code]["tp_level"] = 2
        print("%-8s%8.2f%8.2f%8.2f%8.1f%%%8.1f%%  %s"%(code,cost,last,peak,pdd*100,tdd*100,note or "持有"))
    save_state(st)
    print("="*70)
    if GO and sells:
        from monitoring.alerts import send_alert
        for code,vol,limit in sells:
            oid=c.place_order(code,"sell",vol,limit,"limit")
            print("SELL %s %s@%s -> %s"%(code,vol,limit,oid))
        try: send_alert("[止损/止盈执行] "+", ".join("%s %s股@%s"%(s[0],s[1],s[2]) for s in sells))
        except Exception: pass
    elif sells:
        print("DRY-RUN: 以上%d只触发(止损/止盈), 加 --go 执行"%len(sells))
    else:
        print("无触发")

if __name__=="__main__": main()
