"""tick 采集新鲜度（2026-10-23；Windows 15:10 任务的 Linux 侧只读检查）。

背景：2026-10-23 15:10 的 `Quant_Tick_Collect` 因 xtquant BSON assertion
（0xC0000409）崩溃，20261023 文件缺失且无告警；23:13 重跑成功。本脚本
每天 18:20 SSH 到 Windows 列 `data_store/intraday/tick/*.parquet`，与上一
交易日对比；**缺失时自动触发一次 `Quant_Tick_Collect` 并轮询 6 分钟**，
写 `logs/tick_state.json` 供健康行告警。
"""
import json
import subprocess
import sys
import time
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


def _list_files():
    cmd = ["ssh"] + SSH_OPTS + [
        WIN_HOST, "cmd /c dir /b H:\\quant\\data_store\\intraday\\tick 2>nul"]
    # Windows cmd 中文错误信息是 GBK；text=True 默认 UTF-8 会在重试轮询时
    # 解码崩溃，把"文件缺失"变成"检查器自己失效"（2026-10-26 实锤）。
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="gbk",
                       errors="replace", timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:120])
    files = [x.strip() for x in r.stdout.splitlines()
             if x.strip().lower().endswith(".parquet")]
    return sorted(f[:8] for f in files if f[:8].isdigit())


def _read_coverage(day: str):
    """读 Windows 侧覆盖清单；旧文件没有 sidecar 时返回 None。"""
    cmd = ["ssh"] + SSH_OPTS + [
        WIN_HOST,
        f"cmd /c type H:\\quant\\data_store\\intraday\\tick\\{day}_coverage.json",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=20)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def main():
    now = datetime.now()
    cal = load_meta("trade_calendar")
    ds = (sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y%m%d").unique())
          if cal is not None and not cal.empty else [])
    today = now.strftime("%Y%m%d")
    last_td = max((d for d in ds if d <= today), default=None)
    state = {"as_of": now.isoformat(timespec="seconds"),
             "last_trade_day": last_td, "latest_file": None,
             "n_files": 0, "ok": None, "error": "",
             "retried": False, "retry_ok": None, "retry_error": ""}
    try:
        dates = _list_files()
        state["n_files"] = len(dates)
        state["latest_file"] = dates[-1] if dates else None
        state["ok"] = bool(dates and last_td and dates[-1] >= last_td)
        cov = _read_coverage(dates[-1]) if dates else None
        state["coverage"] = cov
        state["coverage_ok"] = None
        if cov:
            req = int(cov.get("requested") or 0)
            ok_n = int(cov.get("ok") or 0)
            state["coverage_ok"] = bool(
                req > 0 and ok_n == req and not cov.get("errors")
            )
        # 自愈：文件缺失时触发一次 Windows 任务，最多轮询 6 分钟
        if state["ok"] is False and last_td:
            state["retried"] = True
            rr = subprocess.run(
                ["ssh"] + SSH_OPTS + [WIN_HOST,
                                      "schtasks /run /tn Quant_Tick_Collect"],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=20)
            if rr.returncode != 0:
                state["retry_error"] = (rr.stderr or rr.stdout)[:120]
            for _ in range(6):
                time.sleep(60)
                d2 = _list_files()
                if d2:
                    state["n_files"] = len(d2)
                    state["latest_file"] = d2[-1]
                if d2 and d2[-1] >= last_td:
                    break
            state["ok"] = bool(state["latest_file"]
                               and state["latest_file"] >= last_td)
            state["retry_ok"] = state["ok"]
            if state["latest_file"]:
                cov2 = _read_coverage(state["latest_file"])
                if cov2:
                    state["coverage"] = cov2
                    req = int(cov2.get("requested") or 0)
                    ok_n = int(cov2.get("ok") or 0)
                    state["coverage_ok"] = bool(
                        req > 0 and ok_n == req and not cov2.get("errors")
                    )
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)[:120]
    OUT.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
