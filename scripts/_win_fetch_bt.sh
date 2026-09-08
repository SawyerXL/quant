#!/bin/bash
# 取回 pTrade 100万回测产物: 交易明细/持仓明细 CSV + 回测日志 (2026-09-08)
DEST=/root/quant/data_store/ptrade_backtest_1m
mkdir -p "$DEST"
OPTS=(-i ~/.ssh/id_rsa -P 2222 -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/*20260908082036.csv' "$DEST/" 2>&1
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/*20260908082032.csv' "$DEST/" 2>&1
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/ptrade_UAT/Logs/2026-09-07.log' "$DEST/" 2>&1
scp "${OPTS[@]}" 'Administrator@127.0.0.1:F:/gjzq/ptrade_UAT/Logs/2026-09-06.log' "$DEST/" 2>&1
ls -la "$DEST"
