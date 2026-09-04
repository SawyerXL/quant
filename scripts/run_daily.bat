@echo off
REM ============================================================
REM  Track A Daily Auto-Execute Script
REM  Triggered by Windows Task Scheduler at 14:30
REM 2026-09-02: git pull 改走 Linux 中转(Windows 直连 GitHub 443 不通)
REM 2026-09-04: 追加现金短债扫尾(cash_sweep, 档位≤0.5分批建仓)
REM ============================================================

cd /d H:\quant

set PYTHONIOENCODING=utf-8

echo [%date% %time%] ===== Start =====

set GIT_SSH_COMMAND=ssh -i C:\Users\Administrator\.ssh\id_ed25519 -o StrictHostKeyChecking=no
git pull linux main >nul 2>&1

python scripts\fetch_and_execute.py --track a
python scripts\fetch_and_execute.py --track cb

REM 现金短债扫尾: 档位≤0.5时闲置现金→511360(单次封顶20万, 分批建仓)
python scripts\cash_sweep.py

python scripts\reconcile.py --track a

python scripts\export_qmt_positions.py --no-push

echo [%date% %time%] ===== Done =====
