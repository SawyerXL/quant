@echo off
REM 调仓日执行脚本（双击运行，或配置到任务计划）
REM 调仓日：每月中旬+月末，14:30 左右手动运行

cd /d H:\quant

echo ========================================
echo  Track A 信号拉取 + QMT 执行
echo  时间: %date% %time%
echo ========================================

REM 先用 dry-run 模式预览，确认无误后去掉 --dry-run 正式执行
python scripts\fetch_and_execute.py --track a --dry-run

echo.
echo 以上为预览模式（--dry-run），确认无误请按任意键正式下单...
echo 如需取消，直接关闭此窗口
pause

python scripts\fetch_and_execute.py --track a

echo.
echo 执行完成，按任意键关闭
pause
