"""执行链全检查(2026-09-24): 柜台连接测试 + 当前持仓拉取 + 预期差量复算。
只读不写。用法: python scripts/_chain_precheck.py
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
WIN_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]
SCP_OPTS = ["-P", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]


def ssh_run(cmd: str, timeout=90):
    r = subprocess.run(["ssh"] + SSH_OPTS + [WIN_HOST, cmd],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def round_lot(q):
    return (int(q) + 5) // 10 * 10


def main():
    print("===== 执行链全检查 2026-09-24 =====")

    # 1. Windows 端 trader 是否含取整修复
    rc, out = ssh_run(
        'findstr /C:"_round_cb_lot" H:\\quant\\execution\\trader.py',
        timeout=30)
    print(f"[1] Windows trader 取整修复: {'✓ 已部署' if rc == 0 and out.strip() else '✗ 缺失!!'}")

    # 2. Windows .env 的 ENV 配置
    rc, out = ssh_run('type H:\\quant\\.env | findstr /I "ENV"', timeout=30)
    env_ok = rc == 0 and "simulation" in out
    print(f"[2] Windows ENV 配置: {'✓ simulation' if env_ok else '✗ 需查: ' + out.strip()[:80]}")

    # 3. 触发 QMT 持仓导出(=柜台连接测试, Matrix 登录验证)
    rc, out = ssh_run(
        f'"{WIN_PY}" H:\\quant\\scripts\\export_qmt_positions.py --no-push',
        timeout=120)
    print(f"[3] QMT 柜台连接+持仓导出: {'✓' if rc == 0 else '✗'} {out.strip()[-150:]}")

    # 4. 拉回持仓
    r = subprocess.run(
        ["scp"] + SCP_OPTS +
        [f"{WIN_HOST}:H:/quant/logs/qmt_positions_latest.json",
         str(ROOT / "logs" / "qmt_positions_latest.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    if r.returncode != 0:
        print(f"[4] 持仓拉取: ✗ {(r.stderr or '')[-150:]}")
        return
    print("[4] 持仓拉取: ✓")

    # 5. 复算预期差量
    pos = json.loads((ROOT / "logs" / "qmt_positions_latest.json").read_text(
        encoding="utf-8"))
    positions = {str(k).split(".")[0]: v
                 for k, v in pos.get("positions", {}).items()}
    orders_today = pos.get("today_orders", [])
    print(f"[5] 快照时点: {pos.get('exported_at')} | 当日已委托: {len(orders_today)} 笔")

    sig = json.loads((ROOT / "data_store/meta/signal_cb_latest.json").read_text(
        encoding="utf-8"))
    holdings = set(sig["holdings"])
    shares = sig["shares"]
    cb_prefix = ("110", "111", "113", "118", "123", "127", "128")
    held_cb = {c: v for c, v in positions.items()
               if c[:3] in cb_prefix and v.get("volume", 0) > 0}

    sells = []
    for c, v in sorted(held_cb.items()):
        if c not in holdings:
            sells.append((c, v["volume"]))
    buys = []
    for c, tgt in sorted(shares.items()):
        cur = held_cb.get(c, {}).get("volume", 0)
        d = round_lot(tgt - cur)
        if d > 0:
            buys.append((c, cur, tgt, d))
    print(f"[6] 预期卖出 {len(sells)} 笔: {sells}")
    print(f"[7] 预期买入 {len(buys)} 笔: {buys}")
    total_sell = sum(v for _, v in sells)
    print(f"[8] 卖出张数合计 {total_sell} | 单笔金额上限检查: "
          + ", ".join(f"{c}{v}张" for c, v in sells))


if __name__ == "__main__":
    main()
