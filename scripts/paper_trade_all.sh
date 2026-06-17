#!/bin/bash
cd /root/quant
source .venv/bin/activate

# Track A (QMT-based)
python scripts/paper_trade_update.py >> logs/cron.log 2>&1

# CSI500纸面
python -c "
import sys; sys.path.insert(0,'/root/quant')
import pandas as pd; from pathlib import Path; from datetime import date
from scripts.paper_trade_update import fetch_prices, calc_pnl

today=date.today().strftime('%Y-%m-%d')
for tag,fn in [('csi500','logs/paper_trade_csi500_start.csv'),('shadow','logs/paper_trade_shadow_start.csv')]:
    start=pd.read_csv(fn,dtype={'代码':str},encoding='utf-8-sig')
    start['代码']=start['代码'].astype(str).str.zfill(6)
    prices=fetch_prices(start['代码'].tolist(),today)
    result=calc_pnl(start,prices,today)
    out=Path(f'logs/paper_trade_{tag}_{today.replace(\"-\",\"\")}.csv')
    result.to_csv(out,index=False,encoding='utf-8-sig')
    summary=result[result['代码']=='合计'].iloc[0]
    print(f'[{tag}] {today} 市值{summary[\"当前市值(元)\"]:,.0f} 盈亏{summary[\"浮动盈亏(元)\"]:+,.0f}')
" >> logs/cron.log 2>&1
