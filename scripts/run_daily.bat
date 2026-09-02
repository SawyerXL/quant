@echo off
REM ============================================================
REM  Track A Daily Auto-Execute Script
REM  Triggered by Windows Task Scheduler at 14:30
REM  - Normal day: signal not fresh, auto-skip
REM  - Rebalancing day: execute trades
REM  - MA10 exit day: execute exits
REM 2026-09-02: git pull 改走 Linux 中转(Windows 直连 GitHub 443 不通)
REM ============================================================

cd /d H:\quant

REM 用utf-8输出, 否则reconcile/fetch里的✅等字符在GBK控制台会 UnicodeEncodeError 崩溃
set PYTHONIOENCODING=utf-8

echo [%date% %time%] ===== Start =====

REM Pull latest code from Linux relay (GitHub blocked on this host)
set GIT_SSH_COMMAND=ssh -i C:\Users\Administrator\.ssh\id_ed25519 -o StrictHostKeyChecking=no
git pull linux main >nul 2>&1

REM Execute signal (freshness check: auto-skip on non-rebalancing days)
python scripts\fetch_and_execute.py --track a
python scripts\fetch_and_execute.py --track cb

REM Reconcile after execution
python scripts\reconcile.py --track a

REM Save daily snapshot locally (Windows archive, survives tunnel outages)
python scripts\export_qmt_positions.py --no-push

echo [%date% %time%] ===== Done =====
