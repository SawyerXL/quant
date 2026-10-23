"""tick 采集新鲜度（2026-10-23；Windows 15:10 任务的 Linux 侧只读检查）。

背景：2026-10-23 15:10 的 `Quant_Tick_Collect` 因 xtquant BSON assertion
（0xC0000409）崩溃，20261023 文件缺失且无告警；23:13 重跑成功。本脚本
每天 18:20 SSH 到 Windows 列 `data_store/intraday/tick/*.parquet`，与上一
交易日对比，写 `logs/tick_state.json` 供健康行告警。
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from data.storage import load_meta

WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=8"]
OUT = ROOT / "logs" / "tick_state.json"


def main():
    now = datetime.now()
    cal = load_meta("trade_calendar")
    ds = (sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y%m%d").unique())
          if cal is not None and not cal.empty else [])
    today = now.strftime("%Y%m%d")
    last_td = max((d for d in ds if d <= today), default=None)
    state = {"as_of": now.isoformat(timespec="seconds"),
             "last_trade_day": last_td, "latest_file": None,
             "n_files": 0, "ok": None, "error": ""}
    try:
        cmd = ["ssh"] + SSH_OPTS + [
            WIN_HOST, "cmd /c dir /b H:\\quant\\data_store\\intraday\\tick 2>nul"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[:120])
        files = [x.strip() for x in r.stdout.splitlines()
                 if x.strip().lower().endswith(".parquet")]
        dates = sorted(f[:8] for f in files if f[:8].isdigit())
        state["n_files"] = len(dates)
        state["latest_file"] = dates[-1] if dates else None
        state["ok"] = bool(dates and last_td and dates[-1] >= last_td)
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)[:120]
    OUT.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
