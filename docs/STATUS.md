# 项目状态

## 最新更新：2026-05-17（策略A-3正式部署）

---

## 当前阶段：策略A-3已部署，准备接入QMT模拟盘

### 本周完成（2026-05-17）

**策略研发（完整迭代路径）**

| 版本 | 核心内容 | 夏普 | 年化 | 最大回撤 |
|---|---|---|---|---|
| v0.1 | 原始6M动量等权 | 1.19 | 30.4% | -24% |
| v0.2 | 因子优化+科创板手数修正 | 1.20 | 30.6% | -24% |
| v0.3 | 策略A-2：多周期+行业中性+波动率+阶梯仓位 | 1.40 | 26.4% | -18.4% |
| v0.4 | 策略A-3回测：A-2+新仓保护期 | 1.47 | 28.5% | -17.8% |
| **v0.5** | **策略A-3正式接入信号生成** | **1.47** | **28.5%** | **-17.8%** |

**策略A-3 核心参数（生产版）**
```
因子：多周期行业内Z-score
  1M动量(30%) + 6M动量(40%) + 12M动量(30%)
  × 量价突破加成（最高+50%）
  × 波动率倒数（0.7-1.3倍）
  × 成交额权重（截面70% + 行业内30%）

选股：行业均衡（按行业强度分配30只名额，单行业上限8只）
      新仓保护期：持仓<2期的股票需替换者高出15%得分才能换出
权重：得分线性加权 × 主线板块1.3倍
仓位：CSI800/MA200 五档（30%-100%，不完全清仓）
止损：期内-15% / 追踪-18%
调仓：双周（月中+月末，约20次/年）
股票池：CSI800（800只）
```

**保护期设计依据（持仓天数分析）**
```
<2周   胜率23%  单笔期望-4.3%  ← 负收益，需减少
2-4周  胜率33%  单笔期望-3.2%  ← 负收益，需减少
>4周   胜率51%  单笔期望+6.0%  ← 正收益，需增加
→ 保护期让持仓向>4周偏移，提升期望收益
```

**交易统计（策略A-2/A-3基准）**
- 年化换手率：~1010%（约20次调仓/年，每次约15只换仓）
- 个股平均持仓：31天（约1个月）
- 个股胜率：39%（策略A-2）

**信号生成升级**
- `daily_signal_a.py` 已升级为策略A-3
- 新增 `hold_counts` 持仓期数持久化（存入信号JSON，跨调仓日读取）
- 信号文件新增字段：position_ratio（阶梯仓位）、hold_counts、weights

**纸面交易（Track A）**
- 建仓30只，总金额804,192元（以5/11实际收盘价为成本）
- 每日cron 15:30自动更新
- 建仓至今（5/11-5/17）：持续跟踪中

---

### 当前待办（优先级排序）

**P0：下周就做**
1. QMT账户接入确认 → 提供账户ID/路径
2. Windows交易服务器配置（或券商VPS）
3. 配置 `.env` 的 `QMT_ACCOUNT_ID` 和 `QMT_PATH`

**P1：QMT到位后立即做**
4. Windows服务器：克隆仓库、安装依赖、配置.env
5. MockQMTClient → 真实QMT切换测试（改ENV=production）
6. 首次信号执行验证：signal_a_latest.json → trader.py → QMT下单
7. 对账验证：QMT实际持仓 vs 信号文件对齐

**P2：后续**
8. 企业微信机器人WECHAT_WEBHOOK_URL配置
9. Track B 三位一体策略：Track A上实盘后启动

---

## 版本历史

| 标签 | 日期 | 内容 |
|---|---|---|
| v0.1-track-a | 2026-05-11 | 初版回测达标 |
| v0.2-track-a | 2026-05-12 | 因子优化 |
| v0.3-track-a | 2026-05-17 | 策略A-2正式版 |
| v0.4-track-a | 2026-05-17 | 策略A-3回测验证 |
| **v0.5-track-a** | **2026-05-17** | **策略A-3信号生成正式版（当前）** |

---

## QMT接入检查清单（下周用）

```bash
# Windows服务器上执行：
git clone git@github.com:SawyerXL/quant.git
cd quant
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 配置 .env（从 .env.example 复制）
cp .env.example .env
# 填入：QMT_PATH / QMT_ACCOUNT_ID / WECHAT_WEBHOOK_URL

# 测试Mock模式（不实际下单）
python scripts/daily_signal_a.py  # 生成信号
python -c "from execution.trader import Trader; t=Trader(); print(t.client.get_account_info())"

# 切换实盘模式
# 修改 .env: ENV=production
```

---

## 技术快速参考

**服务器**：47.116.166.139（root，SSH免密，`ssh quant-linux`）

**关键文件**
```
data_store/meta/signal_a_latest.json   最新Track A信号（含hold_counts）
logs/paper_trade_YYYYMMDD.csv          每日纸面交易快照
回溯交易记录/回测交易报告.xlsx          回测交易分析（含配对记录+标的汇总）
```

**每日cron**
```
14:25 → daily_signal_a.py    策略A-3信号生成（调仓日才真正输出）
15:30 → paper_trade_update.py 纸面交易跟踪
17:00 → daily_data_update.py  日线数据更新
17:30 → health_check.py       系统健康检查
```

**数据源**：MCP恒生聚源（阿里云IP被东方财富封锁）

**风控红线**：单股≤5% | 单笔≤10万 | 账户回撤≤25% | 涨跌停禁买卖
