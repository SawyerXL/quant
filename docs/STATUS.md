# 项目状态

## 最新更新：2026-05-11

### 当前阶段
- **Week 1 完成**：项目骨架 + 云服务器配置

### 已完成
- [x] 本地项目骨架（40个文件，12个单元测试全通过）
- [x] Git 仓库：github.com/SawyerXL/quant（私有）
- [x] 云服务器配置：47.116.166.139（Ubuntu 24.04，Python 3.12）
- [x] SSH 免密登录（本地 Mac → 服务器）
- [x] Python 虚拟环境 + 所有依赖安装完成
- [x] 恒生聚源 MCP 接口接通（FinQuery工具可用）
- [x] cron 定时任务配置（17:00 数据更新 / 14:50 信号生成）
- [x] 历史数据初始化（init_history.py）**正在运行中**

### 正在进行
- [ ] **init_history.py 后台运行中**（screen session: init）
  - 下载 5513 只股票 2019-2024 日线数据（用 MCP 恒生聚源）
  - 命令：`screen -r init` 查看 / `tail -f logs/init_history_full.log` 看日志
  - 预计完成：约 2026-05-11 02:00

### 待办（按优先级）
- [ ] 确认历史数据下载完成，校验数据质量
- [ ] Track A 多因子策略完整回测（2019-2024）
- [ ] 金标准测试（等权沪深300，验证回测引擎）
- [ ] QMT 券商开通确认
- [ ] Track B 三位一体策略实现（strategies/trinity/）
- [ ] Windows 服务器（QMT交易执行）

### 关键参数
- MCP KEY：在 .env 文件里（不进 git）
- 数据存储：/root/quant/data_store/daily/{year}/{code}.parquet
- 虚拟环境：/root/quant/.venv（激活：source .venv/bin/activate）
- 日志目录：/root/quant/logs/

---

## 架构决策备忘（快速参考）

**策略**：双轨
- Track A：多因子月度选股，60万，8周上实盘，目标年化15-20%
- Track B：三位一体强势股，30万，20周上实盘，目标年化25%+

**数据源**：
- 云服务器用 MCP（东方财富封锁阿里云IP，Akshare在云上不可用）
- 本地开发用 Akshare 或 MCP 均可
- 切换方式：改 .env 里的 DATA_SOURCE

**复权方式**：前复权 qfq（价格接近实际市价）

**关键风控**：
- 单股仓位 Track A ≤5% / Track B ≤8%
- 单笔订单上限 5万（防错单）
- 账户回撤熔断 25%
- 涨停不买，跌停不卖

---

## 历史更新

### 2026-05-10
- 完成项目骨架搭建（全部基础文件）
- 接通恒生聚源 MCP 接口，实现 MCPSource
- 发现阿里云IP被东方财富封锁，改用MCP拉历史数据

### 2026-05-11
- 云服务器配置完成
- 历史数据初始化开始运行
