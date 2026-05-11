#!/bin/bash
# 明天的自动化执行链：等 init 完成 → 数据校验 → 回测
# 由 screen 在后台持续运行

cd /root/quant
source .venv/bin/activate
LOG=logs/tomorrow_chain.log

echo "$(date): 等待 init_history 完成..." >> $LOG

# 每2分钟检查一次 init 是否跑完
while true; do
    if grep -q '初始化完成' logs/init_history_full.log 2>/dev/null; then
        echo "$(date): init_history 完成，开始数据校验" >> $LOG
        break
    fi
    # 如果 screen init 会话已经消失，也视为完成
    if ! screen -ls | grep -q 'init'; then
        echo "$(date): screen init 会话结束，开始数据校验" >> $LOG
        break
    fi
    sleep 120
done

# 步骤1：数据校验
echo "$(date): === 开始数据校验 ===" >> $LOG
python scripts/validate_data.py >> $LOG 2>&1
echo "$(date): 数据校验完成" >> $LOG

# 步骤2：等到早上9点再跑回测（避免凌晨资源争抢）
TARGET=$(date -d 'today 09:00' +%s 2>/dev/null || date -j -f "%H:%M" "09:00" +%s 2>/dev/null)
NOW=$(date +%s)
if [ $NOW -lt $TARGET ]; then
    WAIT=$((TARGET - NOW))
    echo "$(date): 等待到09:00再跑回测（还需等 ${WAIT}秒）" >> $LOG
    sleep $WAIT
fi

# 步骤3：Track A 回测
echo "$(date): === 开始 Track A 回测 ===" >> $LOG
python scripts/run_backtest_a.py >> $LOG 2>&1
echo "$(date): 回测完成，查看结果: cat logs/backtest_a.log" >> $LOG

echo "$(date): === 明天计划全部完成 ===" >> $LOG
