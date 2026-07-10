@echo off
REM ============================================================
REM  Track A Daily Auto-Execute Script
REM  Triggered by Windows Task Scheduler at 14:30
REM  - Normal day: signal not fresh, auto-skip
REM  - Rebalancing day: execute trades
REM  - MA10 exit day: execute exits
REM ============================================================

cd /d H:\quant

REM 用utf-8输出, 否则reconcile/fetch里的✅等字符在GBK控制台会 UnicodeEncodeError 崩溃
set PYTHONIOENCODING=utf-8

echo [%date% %time%] ===== Start =====

REM Pull latest code and signal
git pull origin main >nul 2>&1

REM Execute signal (freshness check: auto-skip on non-rebalancing days)
python scripts\fetch_and_execute.py --track a

REM Reconcile after execution
python scripts\reconcile.py --track a

echo [%date% %time%] ===== Done =====
