#!/bin/bash
# 校验 Windows 上策略文件与本地补丁版一致 (大小+md5+修改时间)
ssh -i ~/.ssh/id_rsa -p 2222 -o BatchMode=yes -o ConnectTimeout=15 \
    -o StrictHostKeyChecking=no Administrator@127.0.0.1 \
    'powershell -NoProfile -Command "Get-Item F:\gjzq\ptrade_main_sim_1m.py | Select-Object Length, LastWriteTime; (Get-FileHash F:\gjzq\ptrade_main_sim_1m.py -Algorithm MD5).Hash"'
