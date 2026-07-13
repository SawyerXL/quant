"""
QMT终端连接探针 — 跑在 Windows QMT 机。
尝试连接申万宏源 Matrix 终端, 打印一行状态供 Linux 侧告警判断。
终端没登录时 get_client() 会抛 RuntimeError(result=-1) → 打印 FAIL。

输出(stdout最后一行):
  QMT_CONN_OK npos=<持仓数>        # 已连接
  QMT_CONN_FAIL <错误>            # 未连接/未登录
用法: python scripts/qmt_conn_probe.py   (exit 0=连上, 1=没连上)
"""
import os, sys
os.environ["ENV"] = "simulation"
sys.path.insert(0, "H:/quant")

try:
    from execution.qmt_client import get_client
    c = get_client()
    pos = c.get_positions()
    npos = len([p for p in pos.values() if isinstance(p, dict) and p.get("volume", 0) > 0])
    print(f"QMT_CONN_OK npos={npos}")
    sys.exit(0)
except Exception as e:
    print(f"QMT_CONN_FAIL {repr(e)[:200]}")
    sys.exit(1)
