"""
QMT快照 + NAV追踪 — Linux主动拉取(替代不稳定的Windows推送)
用法: python scripts/qmt_snapshot.py
"""
import sys, json, subprocess, shutil, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/qmt_snapshot.log", rotation="7 days")

WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
# Windows 上必须用装了 xtquant 的那个解释器全路径; 裸 python 在非交互 ssh 里不是它 → export 静默失败
WIN_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
WIN_FILE = "H:/quant/logs/qmt_positions_latest.json"
LOCAL_LATEST = Path("logs/qmt_positions_latest.json")
PERF_FILE = Path("logs/qmt_performance.csv")
NAV_FILE = Path("logs/qmt_nav_history.parquet")

SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
# scp 的端口flag是 -P(大写); 用 ssh 的 -p 会被当成 preserve, 端口号变成多余参数 → 拉取失败
SCP_OPTS = ["-P", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

def trigger_export():
    cmd = ["ssh"] + SSH_OPTS + [WIN_HOST, f'"{WIN_PY}" H:\\quant\\scripts\\export_qmt_positions.py --no-push']
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    if r.returncode != 0:
        logger.error(f"Windows导出失败(rc={r.returncode}): {(r.stderr or r.stdout)[-300:]}")
    return r.returncode == 0

def pull_file():
    cmd = ["scp"] + SCP_OPTS + [f"{WIN_HOST}:{WIN_FILE}", str(LOCAL_LATEST)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        logger.error(f"scp拉取失败(rc={r.returncode}): {(r.stderr or '')[-200:]}")
    return r.returncode == 0

def update_nav():
    """归档当日快照, 然后用 qmt_nav_track 重建正确的 mark-to-market NAV。
    旧实现按 market_value 差值算日收益(被买卖污染, daily_ret 出现6459%这种垃圾), 已弃用。"""
    if not LOCAL_LATEST.exists():
        return
    d = json.loads(LOCAL_LATEST.read_text(encoding="utf-8"))
    today = date.today()

    # 归档当日快照(用刚拉到的最新数据覆盖, 收盘15:45这次为准)
    snap_file = Path(f"logs/qmt_positions_{today.strftime('%Y%m%d')}.json")
    shutil.copy(LOCAL_LATEST, snap_file)

    # 重建 NAV(忽略污染的 total_assets, 用 holdings×本地收盘 + 100万notional)
    from qmt_nav_track import rebuild_and_save
    nav = rebuild_and_save()
    if nav is not None and len(nav):
        last = nav.iloc[-1]
        logger.info(f"NAV: {last['n_pos']}只 投入{last['invested_pct']:.0f}% "
                    f"NAV={last['nav']:.4f} 累计{last['nav']-1:+.2%}")


def backfill_exec_record():
    """收盘后补拉成交数据, 覆盖执行记录中的fill_rate/滑点。"""
    cmd = ["ssh"] + SSH_OPTS + [WIN_HOST,
           f'"{WIN_PY}" H:/quant/scripts/fetch_and_execute.py --backfill']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if r.returncode == 0:
            logger.info("成交补拉完成")
        else:
            logger.warning(f"成交补拉失败(rc={r.returncode}): {(r.stderr or r.stdout)[-200:]}")
    except Exception as e:
        logger.warning(f"成交补拉异常: {e}")

def run():
    logger.info("QMT snapshot...")
    if trigger_export() and pull_file():
        update_nav()
        backfill_exec_record()  # 收盘后补拉成交数据覆盖fill_rate/滑点
        logger.info("Done")

if __name__ == "__main__":
    run()
