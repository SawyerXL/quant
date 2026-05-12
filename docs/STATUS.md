# 项目状态

## 最新更新：2026-05-12

---

## 当前阶段：Week 2 完成，等待 QMT 信息

### 已完成（截至2026-05-12）

**数据层**
- [x] 5487/5513只股票日线数据（2019至今）
- [x] 财务数据：787/800只（CSI800成分股季报）
- [x] 数据校验PASS（85.4%覆盖率，基准价格验证通过）

**Track A 回测**
- [x] 回测达标：年化30.4%，夏普1.19，最大回撤-24%（Formula I）
- [x] 回溯交易记录：147次调仓，3394笔记录 → `/回溯交易记录/` 文件夹
- [x] **新**：因子优化：截面成交额排名改为 板块内排名30% + 截面70%（修正行业规模偏差）
- [x] **新**：科创板最小手数修正：688开头→200股，其余→100股（daily_signal_a.py + trader.py）

**Track B 方向确定**
- [x] 修复3个前视偏差bug后量化回测结论：年化-29.3%（符合预期，量化无法单独跑通）
- [x] **决策（2026-05-12）：选择路线B** — 人机协作模式
  - 大势层+板块层：金融同学每周人工判断，填入 `manual_scores_b.json`
  - 个股层：量化强势打分（资金/价格/趋势三维）自动输出
  - 量化回测不是验证方式，改用纸面交易验证人工判断质量
- [ ] Track B纸面交易：等Track A上实盘后（约8周后）启动

**纸面交易**
- [x] 已建仓30只，总金额77万
- [x] 建仓当日盈亏：+29951元（+1.53%）
- [x] cron 15:30 每日自动更新，结果存 `logs/paper_trade_YYYYMMDD.csv`

**系统**
- [x] 云服务器配置完成（47.116.166.139）
- [x] cron 14:25 生成信号，17:00 更新数据，15:30 纸面交易跟踪
- [x] 最新信号（5/11）：bull 大势，30只持仓

### 当前待办

**团队来做**：
1. 联系QMT券商确认账户审批状态
2. 询问券商是否提供配套Windows VPS
3. 配置企业微信机器人 webhook（WECHAT_WEBHOOK_URL），接收每日告警

**Track B人工操作（每周一早上，金融同学）**：
填写 `data_store/meta/manual_scores_b.json`：
```json
{
  "week_start": "YYYY-MM-DD",
  "market_manual_score": 70,
  "sector_overrides": {
    "电子": 85,
    "医药生物": 40
  },
  "notes": "本周市场判断说明"
}
```

---

## 接下来的4周路线图

| 周次 | 关键任务 | 依赖 |
|---|---|---|
| Week 3（本周）| Track A回测验证✅，QMT信息确认，Track B纸面交易人工输入启动 | QMT状态确认 |
| Week 4 | QMT安装测试，MockQMTClient模拟交易链路验证 | Windows服务器 |
| Week 5-6 | QMT模拟盘，信号→下单→对账全流程 | QMT连接 |
| Week 7-8 | 10万→60万逐步实盘；Track B同步纸面交易积累数据 | 模拟盘通过 |

---

## 技术架构快速参考

**数据源**：云服务器用MCP（阿里云IP被东方财富封锁，Akshare不可用）

**cron任务**：
```
14:25 每日  → daily_signal_a.py  Track A信号生成（月末/月中才输出新信号）
14:25 周五  → daily_signal_b.py  Track B信号生成
15:30 每日  → paper_trade_update.py  纸面交易跟踪
17:00 每日  → daily_data_update.py  日线数据更新
17:30 每日  → health_check.py  系统健康检查
```

**关键文件**：
```
data_store/meta/signal_a_latest.json  → 最新Track A信号
logs/paper_trade_YYYYMMDD.csv        → 每日纸面交易快照
logs/backtest_a.log                  → 回测结果
logs/trades_a_detail.csv             → 详细调仓记录
```

**风控红线**：单股≤5%（TrackA）/ 8%（TrackB）| 单笔≤10万 | 账户回撤≤25%

---

## 版本历史

| 版本 | 日期 | 主要内容 |
|---|---|---|
| v0.1-track-a | 2026-05-11 | Track A回测达标版（年化30.4%，夏普1.19） |
| v0.2-track-a | 待打标 | 因子优化+科创板手数修正后重跑达标 |
