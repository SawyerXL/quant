# QMT 内置 Python 移植计划（2026-11-02）

> 券商最终确认：**只能用 QMT 内置 Python**。外部 Python/xtquant 禁用；
> 现产 `fetch_and_execute`/`qmt_client` 链不再作为执行路径。
> QMT 仍是主执行平台，不转 pTrade；100 万切换冻结，日期待重排。

## 1. 现状与硬约束

- **现有执行链保留**：`execution/qmt_client.py`、`fetch_and_execute.py`、
  `trader.py`、`cash_sweep`、`qmt_snapshot` 等不修改、不删除、不改变默认
  行为；内置模型移植在独立 `qmt_builtin/` 目录/独立模型中并行开发。
  外部 xtquant 仍是“现状/参考”，但**不作为最终交付承诺**；不用于新增
  真实下单，100 万冻结。
- 内置模型：QMT 客户端内 Python 3.6.8；`init(ContextInfo)` /
  `handlebar(ContextInfo)`；`passorder` 下单、`get_trade_detail_data`
  查持仓/委托、`run_time` 定时、`load_stk_list`/`load_stk_vol_list`
  读本地篮子文件。
- **信号格式用 CSV 篮子**（`code,volume`）；方向由“目标持仓 vs 当前持仓”推导，
  价格/涨跌停/风控在模型内算。
- **2026-11-02 实测：`ContextInfo.load_stk_vol_list` 会重命名/消费源文件**
  （`target_a_g0.csv` → `target_a_g0<timestamp>_4.csv`），不能每轮重读同文件。
  P1 改用标准 `open()` 读 CSV；仿真下 `open()` 可用（heartbeat_ok），
  backtest 下被沙箱禁止（Foribdden FileIO）。生产需再测 live `open()`；
  若 live 不允许，改用 QMT 篮子管理/一次性读入缓存。
- 系统说明文档 §3.6.3.1.5：QMT 客户端支持**开机自动启动/自动重启**；
  但“模型在客户端重启后是否自动恢复运行”文档未明说，P1 实测。
- 模型必须在 QMT 客户端本地运行；Linux 只做研究/信号，不能直接触发 QMT 代码。
- Python API §3.1：内置 NumPy/Pandas/Patsy/SciPy/Statsmodels/TA_Lib；
  另装第三方库到 `bin.x64/Lib/site-packages`，且需券商管理端白名单。

## 2. 移植范围（不能只改配置）

1. **信号契约**：Linux JSON → 本地 CSV/篮子（代码/方向/数量/限价/
   tier/日期）；QMT 模型读取并校验。
2. **执行核心**：`fetch_and_execute` 的目标 diff、分笔/取整、tick 归一、
   涨跌停禁买禁卖、单笔 10 万、单股 5%/8%、行业 30%/50%、15% 熔断、
   可用资金、人工锁、委托状态/废单检查。
3. **交易轨**：track_a（2×20 万 g0/g1）、track_cb、40/60、MA10、
   `cash_sweep`、002028、止损/止盈。
4. **快照/对账**：`get_trade_detail_data` → 本地持仓/NAV/订单文件；
   Linux 拉取做健康行、FWD-002 对账、15:40 验证。
5. **调度**：QMT 模型 `run_time` 覆盖 14:25/14:30/14:45/14:50/15:05；
   模型必须常驻；开机自启用 Windows 本地任务，不用 SSH 远程触发。

## 3. 阶段

- **P0（已完成）**：保留现有执行链原样；建立独立移植目录/文档；不改账户。
- **P1 只读 spike**：内置模型用 `load_stk_vol_list` 读本地篮子 CSV、
  `get_trade_detail_data` 打日志、`run_time` 定时；不调用 `passorder`；
  同时测试 `open()` 写本地日志/快照、客户端重启后模型是否自动恢复。
  需 QMT 模型交易权限/登录。
- **P2 风控/下单**：移植纯函数并写 Python 3.6 兼容单测；QMT 仿真小额
  `passorder`；验废单/tick/涨跌停/在途修正。
- **P3 全链**：双轨、CB、cash_sweep、002028、快照、健康、对账；
  与回测/现有工件做逐位对账。
- **P4 演练**：至少 2 周仿真（无真实资金）→ 小资金 → 用户批准才谈 100 万。

## 4. 验收（每阶段）

- 代码单测通过；QMT 仿真有真实委托/成交；失败分支有已知失败用例；
- 计划/成交/持仓/NAV 对账；健康行可见；无外部 xtquant 调用；
- 任何阶段未过闸门不得进入下一阶段；100 万不得提前。

## 5. 文档已答 vs 仍需券商/实测

**文档已答**：
- 信号文件：`load_stk_list`/`load_stk_vol_list` 支持本地篮子 txt/csv；
  JSON/标准 `open()` 未文档化 → 主路径用篮子 CSV。
- 第三方库：内置 NumPy/Pandas/Patsy/SciPy/Statsmodels/TA_Lib；自定义
  库可装到 `bin.x64/Lib/site-packages`，但需券商管理端白名单。
- 客户端支持开机自动启动/自动重启（系统说明 §3.6.3.1.5）；模型自身
  重启后是否自动恢复，文档未明说 → P1 实测。
- 模拟/实盘下单必须把模型加入“**模型交易**”，不是普通“运行”。

**仍需确认/实测**：
1. 模型交易（模拟/实盘）的账户权限是否就是之前“要开权限”那一项？
2. Windows 云服务器是否算券商定义的“本地”（书面确认）。
3. 内置模型写本地日志/快照（标准 `open()`）是否允许；若不允许，用
   QMT 日志/`customDll`/Windows 本地文件监控替代。
4. 客户端重启后模型是否自动恢复“运行中”；若不，Windows 任务计划
   能否自动登录并启动模型。

**结论**：这不是“切个开关”，而是把执行/风控/对账链重做一遍；在 P2
通过前，100 万和所有外部 QMT 下单保持冻结。
