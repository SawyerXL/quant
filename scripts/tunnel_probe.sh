#!/bin/bash
# 隧道探针(2026-09-02): Windows keepalive 经主SSH调用本脚本,
# 本脚本再经2222反向隧道连回Windows —— 验证完整链路。
# 独立成脚本是为了避开 PowerShell→ssh 嵌套引号传参被搅碎的问题。
ssh -i ~/.ssh/id_rsa -p 2222 -o BatchMode=yes -o ConnectTimeout=8 \
    -o StrictHostKeyChecking=no Administrator@127.0.0.1 'echo TUNNEL_OK'
