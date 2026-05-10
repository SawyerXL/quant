# 项目状态

## 最新更新：2026-05-10

### 当前阶段
- **Week 1**：项目骨架搭建完成

### 已完成
- [x] 目录结构
- [x] requirements.txt / .gitignore / .env.example / CLAUDE.md
- [x] config/settings.py + 双策略参数文件
- [x] DataSource 抽象基类 + AkshareSource 实现 + MCPSource 存根
- [x] Parquet 存储工具（data/storage.py）
- [x] 数据清洗（data/cleaner.py）
- [x] 因子库：价值/质量/动量/情绪/工具函数
- [x] Track A 多因子策略主体（strategies/multi_factor.py）
- [x] 回测引擎 vectorbt 封装（backtest/engine.py）
- [x] 风控网关（execution/risk.py）
- [x] QMT 客户端 + Mock（execution/qmt_client.py）
- [x] 交易执行器（execution/trader.py）
- [x] 企业微信告警（monitoring/alerts.py）
- [x] 每日数据更新脚本（scripts/daily_data_update.py）
- [x] Track A 信号生成脚本（scripts/daily_signal_a.py）
- [x] 单元测试框架（tests/test_factors.py）

### 待办（按优先级）
- [ ] 安装依赖（pip install -r requirements.txt）并跑通导入测试
- [ ] 确认 MCP 接口字段清单，实现 MCPSource（替换存根）
- [ ] 初始化历史数据（5年全A日线 + 财务 + 行业分类）
- [ ] 金标准回测测试（backtest/engine.py 的 gold_standard_test）
- [ ] Track A 完整回测（2019-2024）
- [ ] Track B 策略设计实现（strategies/trinity/）
- [ ] QMT 模拟盘对接（需要 Windows 服务器）
- [ ] 部署脚本（deploy.sh）

### 阻塞项
- MCP 接口字段未确认 → 使用 Akshare 兜底
- QMT 开通申请中 → 使用 MockQMTClient 开发

---

## 历史更新

### 2026-05-10
- 完成项目骨架搭建（全部基础文件）
