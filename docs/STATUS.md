# 项目状态

## 最新更新：2026-05-11 00:40

### 当前阶段
- **Week 1 完成**，**Week 2 自动化执行中**

### 正在自动运行（无需人工干预）

服务器上两个 screen 会话：

| 会话名 | 任务 | 状态 |
|---|---|---|
| `init`  | 下载 5513 只股票 2019 年至今日线数据（MCP） | 运行中，预计 ~03:30 完成 |
| `chain` | 等 init 完成 → 数据校验 → 09:00 → Track A 回测 | 等待中 |

查看进度：
```bash
tail -20 logs/init_history_full.log   # 下载进度
cat logs/tomorrow_chain.log           # 链条状态
cat logs/validate_data.log            # 校验结果（init后生成）
grep -A20 '总体指标' logs/backtest_a.log  # 回测结果（09:00后生成）
```

### 已完成

**基础设施（Week 1）**
- [x] 项目代码骨架（40个文件，12个单元测试全通过）
- [x] GitHub 私有仓库：github.com/SawyerXL/quant
- [x] 云服务器 47.116.166.139（Ubuntu 24.04，Python 3.12）
- [x] SSH 免密登录 / cron 定时任务 / Claude Code 安装
- [x] 恒生聚源 MCP 接口接通并验证
- [x] 发现并解决：阿里云 IP 被东方财富封锁 → 改用 MCP 拉数据
- [x] 三份 Word 方案文档（项目方案目录）
- [x] 数据校验脚本 validate_data.py
- [x] Track A 回测脚本 run_backtest_a.py（纯 pandas/numpy，不依赖 vectorbt）
- [x] 自动化链条脚本 tomorrow_chain.sh 已在服务器后台运行

### 待办（按优先级）
- [ ] 查看回测结果（明天白天自动完成）
- [ ] 根据回测结果决定是否进入模拟盘
- [ ] 确认 QMT 券商账户开通状态
- [ ] Windows 交易服务器采购
- [ ] Track B 三位一体策略实现（strategies/trinity/）

---

## 关键技术决策（快速参考）

**策略**：双轨
- Track A：多因子月度选股，60万，8周上实盘，目标年化≥15%，最大回撤≤25%，夏普≥1.0
- Track B：三位一体强势股，30万，20周上实盘，目标年化≥25%

**数据源**：
- 云服务器：MCP 恒生聚源（阿里云 IP 被东方财富封锁，Akshare 无法用）
- 本地开发：Akshare 或 MCP 均可
- 切换方式：改 .env 里 DATA_SOURCE

**复权方式**：前复权 qfq（价格接近实际市价）

**回测实现**：纯 pandas/numpy（vectorbt 1.0.0 API 与设计时不同，待验证）

**风控红线**：
- 单股仓位 Track A ≤5% / Track B ≤8%
- 单笔订单上限 5万
- 账户回撤熔断 25%
- 涨停不买，跌停不卖

---

## 历史更新

### 2026-05-10
- 完成项目骨架搭建
- 接通恒生聚源 MCP，实现 MCPSource
- 发现阿里云 IP 被东方财富封锁，改用 MCP

### 2026-05-11
- 云服务器配置完成
- 历史数据初始化开始（MCP，5513只，2019至今）
- 部署 validate_data.py + run_backtest_a.py
- 设置自动化链条（init → 校验 → 回测），明天全天自动运行
