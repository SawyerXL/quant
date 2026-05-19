@echo off
REM ============================================================
REM  Track A 每日自动执行脚本
REM  由 Windows 任务计划程序在 14:30 自动触发
REM  - 普通交易日：信号不新鲜，自动跳过
REM  - 调仓日（月中/月末）：执行换仓
REM  - MA10出清日：执行出清
REM ============================================================

cd /d H:\quant

echo [%date% %time%] ===== 开始执行 =====

REM 拉取最新代码和信号
git pull origin main >nul 2>&1

REM 执行信号（fresh check 保护，非调仓日自动跳过）
python scripts\fetch_and_execute.py --track a

REM 执行完成后对账（仅在有实际下单时有意义，其他时候也无害）
python scripts\reconcile.py --track a

echo [%date% %time%] ===== 执行完毕 =====
