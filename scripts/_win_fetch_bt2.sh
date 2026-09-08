#!/bin/bash
# 取回第二轮回测产物: 全部 交易详情/持仓明细 CSV (新旧都取, 本地按时间挑最新)
DEST=/root/quant/data_store/ptrade_backtest_1m/run2
mkdir -p "$DEST"
OPTS=(-i ~/.ssh/id_rsa -P 2222 -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/*交易*.csv' "$DEST/" 2>&1
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/*持仓*.csv' "$DEST/" 2>&1
ls -lat "$DEST" | head
