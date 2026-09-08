#!/bin/bash
# 推送补丁版 pTrade 回测策略到 Windows F:\gjzq 根目录(用户重跑前从此处粘贴)
scp -i ~/.ssh/id_rsa -P 2222 -o BatchMode=yes -o ConnectTimeout=20 \
    -o StrictHostKeyChecking=no \
    /root/quant/strategies/ptrade_main_sim_1m.py \
    'Administrator@127.0.0.1:F:/gjzq/ptrade_main_sim_1m.py'
