"""做T验证期到期裁决 (2026-09-17, 迁移系统 cron 版, 09-24 16:13 跑)。

判据锁于做T铁律(2026-08-23/08-28/09-03 修订): 含强平全口径,
胜率≥90% 且 单次≥+0.15% → 达标(全仓推广); 否则不达标(查执行
纪律)。样本<10 笔 → 如实报"样本不足, 维持试点不推广"(数字不编)。
数据源: Windows config/t_shadow_stop_log.csv(影子止损, 只记不执行),
scp 只读拉取。无人值守: 结论写账本+邮件必达。
用法: python scripts/t0_verdict.py
"""
import csv
import io
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from loguru import logger

WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
SCP_OPTS = ["-P", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]
WIN_FILE = "H:/quant/config/t_shadow_stop_log.csv"
LOCAL_ARCHIVE = Path("logs/t_shadow_stop_archive.json")
LEDGER = Path("logs/overnight_open_questions.md")

WIN_RATE = 0.90
MIN_PNL = 0.15
MIN_N = 10


def pull_csv() -> str | None:
    # ssh+PowerShell 读文件(scp 在部分环境被拦截, 统一走 ssh)
    cmd = ["ssh", "-p", WIN_PORT, "-i", SSH_KEY,
           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
           WIN_HOST,
           "powershell -Command \"Get-Content -Raw "
           "H:\\quant\\config\\t_shadow_stop_log.csv\""]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.decode("utf-8-sig", errors="replace")
        logger.warning(f"拉取失败 rc={r.returncode}: {(r.stderr or b'')[:200]!r}")
        return None
    except Exception as e:
        logger.warning(f"拉取异常: {e}")
        return None


def main():
    today = date.today().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M:%S")
    raw = pull_csv()
    if raw is None:
        rec = (f"\n## 做T验证期裁决（{today} {ts}, 系统cron）\n"
               f"影子日志拉取失败(隧道/文件缺失) → 裁决不可行, 须人工补\n")
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(rec)
        print(rec)
        sys.exit(1)

    rows = [r for r in csv.DictReader(io.StringIO(raw)) if r.get("date")]
    n = len(rows)
    pnls = [float(r.get("real_pnl_pct") or 0.0) for r in rows]
    touched = sum(1 for r in rows if str(r.get("shadow_touched")).lower() == "true")
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n if n else 0.0
    avg_pnl = sum(pnls) / n if n else 0.0

    if n < MIN_N:
        verdict = (f"样本 {n} 笔 < {MIN_N} → 样本不足, 维持 1/3 仓位试点, "
                   f"不推广(数字不编)")
    elif win_rate >= WIN_RATE and avg_pnl >= MIN_PNL:
        verdict = (f"达标(胜率{win_rate:.0%}≥{WIN_RATE:.0%} 且 "
                   f"单次{avg_pnl:+.2f}%≥+{MIN_PNL}%) → 全仓推广")
    else:
        verdict = (f"不达标(胜率{win_rate:.0%}/{单次avg_pnl:+.2f}%) → "
                   f"查执行纪律, 维持试点")

    rec = (f"\n## 做T验证期裁决（{today} {ts}, 系统cron）\n"
           f"样本 {n} 笔 | 胜率 {win_rate:.0%} | 单次 {avg_pnl:+.3f}% "
           f"(含强平全口径) | 影子触发 {touched} 笔\n判定: {verdict}\n")
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(rec)
    LOCAL_ARCHIVE.write_text(json.dumps(
        {"ts": ts, "n": n, "win_rate": win_rate, "avg_pnl": avg_pnl,
         "touched": touched, "rows": rows}, ensure_ascii=False, indent=1))
    print(rec)
    try:
        from monitoring.alerts import send_alert
        send_alert(f"[做T裁决] {verdict}", level="warning")
    except Exception as e:
        logger.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    main()
