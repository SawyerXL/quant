# 运营操作手册

## 一、环境初始化（首次部署）

```bash
# 1. 克隆仓库
git clone <your-private-repo> quant && cd quant

# 2. 创建虚拟环境
python3.11 -m venv .venv && source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入真实密钥

# 5. 初始化历史数据（首次，约需 2-4 小时）
python scripts/init_history.py --start 2019-01-01

# 6. 验证
pytest tests/ -v
python -c "from data.source import get_source; src=get_source(); print(src.get_trade_calendar()[-3:])"
```

## 二、每日操作

| 时间 | 操作 | 执行方式 |
|---|---|---|
| 09:25 | 检查数据是否更新（前日） | 自动（cron 17:00 跑） |
| 09:35 | Track A 执行调仓（月末） | 自动（信号文件 → QMT） |
| 14:50 | 人工判定情绪周期阶段 | 手动输入到系统（5分钟） |
| 14:55 | Track B 信号生成+执行 | 自动（依赖人工输入） |
| 17:00 | 数据更新 | 自动（cron） |
| 17:30 | 查看企业微信日报 | 被动接收 |

## 三、Linux 服务器 cron 配置

```bash
crontab -e
# 添加以下内容：
0 17 * * 1-5 /home/quant/.venv/bin/python /home/quant/quant/scripts/daily_data_update.py
50 14 * * 1-5 /home/quant/.venv/bin/python /home/quant/quant/scripts/daily_signal_a.py
```

## 四、紧急操作

### 紧急全部清仓
```python
# 在 Windows 服务器上执行
from execution.qmt_client import get_client
client = get_client()
positions = client.get_positions()
prices    = {...}  # 从 QMT 获取当前价格
for code, pos in positions.items():
    client.place_order(code, "sell", pos["volume"], prices[code] * 0.995)
```

### 手动补拉某只股票数据
```python
from data.source import get_source
from data.storage import save_daily
src = get_source()
df  = src.get_daily("000001", "2019-01-01", "2024-12-31")
save_daily("000001", df)
```

### 查看当前持仓
```python
from execution.qmt_client import get_client
client = get_client()
print(client.get_positions())
print(client.get_account_info())
```

### 重新生成 Track A 信号
```python
import sys; sys.path.insert(0, ".")
from strategies.multi_factor import MultiFactor
s = MultiFactor()
codes = s.generate_signal("2024-04-30")
print(codes)
```

## 五、常见问题

**Q: 数据更新失败了**
A: 查看 `logs/data_update_YYYY-MM-DD.log`，通常是 akshare 接口限流，等10分钟后手动重跑。

**Q: 信号生成了但 QMT 没下单**
A: 检查 `data_store/meta/signal_a_latest.json` 是否存在，再检查 Windows 任务计划程序日志。

**Q: 账户回撤触及熔断**
A: 系统会自动停止开仓并推送告警。需要人工复盘后在 .env 中重置 `CIRCUIT_BREAKER_RESET=true` 才能恢复。

**Q: 想换数据源（MCP 接口到位了）**
A: 修改 `.env` 中 `DATA_SOURCE=mcp`，实现 `data/source/mcp_source.py`，重启即可。
