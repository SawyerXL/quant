"""
修复CSI800面板里的脏价格(源/量纲混写导致的数量级跳变)。
对每只脏股: 用干净的新浪qfq重拉整段历史, 整体覆盖各年parquet(不merge,避免保留旧脏行)。
用法: python scripts/fix_dirty_prices.py
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from pathlib import Path
from run_backtest_a import load_panels
from data.storage import load_meta
from data.source import get_source

END="2026-06-30"
src=get_source()
codes=sorted(load_meta("csi800")["code"].tolist())
panel,_=load_panels(codes,"2019-01-01",END)
rets=panel.pct_change(fill_method=None)
dirty=sorted(rets.columns[(rets.abs()>0.5).any()].tolist())
print(f"脏股 {len(dirty)} 只: {dirty}", flush=True)

fixed=[]; failed=[]
for code in dirty:
    df=src.get_daily(code,"2019-01-01",END)
    if df.empty or "close" not in df.columns:
        failed.append(code); print(f"  {code}: 重拉失败(空)", flush=True); continue
    df["date"]=pd.to_datetime(df["date"])
    df=df.dropna(subset=["date"]).sort_values("date")
    # 整体覆盖各年(直接写,不经save_daily的merge)
    for yr,grp in df.groupby(df["date"].dt.year):
        p=Path(f"data_store/daily/{yr}")
        p.mkdir(parents=True,exist_ok=True)
        grp.to_parquet(p/f"{code}.parquet",index=False)
    fixed.append(code)
    print(f"  {code}: 重写 {len(df)}行 ({df['date'].min().date()}~{df['date'].max().date()})", flush=True)

# 重扫验证
print("\n=== 重扫验证 ===", flush=True)
panel2,_=load_panels(codes,"2019-01-01",END)
rets2=panel2.pct_change(fill_method=None)
still=sorted(rets2.columns[(rets2.abs()>0.5).any()].tolist())
print(f"修复后仍有>50%脏跳的股票: {len(still)} 只: {still}")
# 抽查601881
if "601881" in panel2.columns:
    s=panel2["601881"].loc["2026-05-01":"2026-06-18"].dropna()
    print(f"\n601881 修复后近期价(应稳定~12): min={s.min():.2f} max={s.max():.2f}")
print(f"\n修复{len(fixed)}只, 失败{len(failed)}只{failed if failed else ''}")
