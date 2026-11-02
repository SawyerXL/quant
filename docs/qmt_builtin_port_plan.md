# QMT 内置 Python 移植计划（2026-11-02）

> 券商最终确认：**只能用 QMT 内置 Python**。外部 Python/xtquant 禁用；
> 现产 `fetch_and_execute`/`qmt_client` 链不再作为执行路径。
> QMT 仍是主执行平台，不转 pTrade；100 万切换冻结，日期待重排。

## 1. 现状与硬约束

- 已加 `config/qmt_builtin_only.flag`：simulation/production 下
  `get_client()` fail-closed；development 仍 Mock（测试用）。
- 内置模型：QMT 客户端内 Python 3.6.8；`init(ContextInfo)` /
  `handlebar(ContextInfo)`；`passorder` 下单、`get_trade_detail_data`
  查持仓/委托、`run_time` 定时、`load_stk_list`/`load_stk_vol_list`
  读本地篮子文件。
- 文档未保证标准 `open()` 读 JSON；优先用 CSV/篮子格式。
- 模型必须在 QMT 客户端本地运行；Linux 只做研究/信号，不能直接触发 QMT 代码。

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

- **P0（已完成）**：外部链 fail-closed；文档/记忆；不改账户。
- **P1 只读 spike**：内置模型读本地 CSV、`get_trade_detail_data` 打日志、
  `run_time` 定时；不调用 `passorder`。需 QMT 模型交易权限/登录。
- **P2 风控/下单**：移植纯函数并写 Python 3.6 兼容单测；QMT 仿真小额
  `passorder`；验废单/tick/涨跌停/在途修正。
- **P3 全链**：双轨、CB、cash_sweep、002028、快照、健康、对账；
  与回测/现有工件做逐位对账。
- **P4 演练**：至少 2 周仿真（无真实资金）→ 小资金 → 用户批准才谈 100 万。

## 4. 验收（每阶段）

- 代码单测通过；QMT 仿真有真实委托/成交；失败分支有已知失败用例；
- 计划/成交/持仓/NAV 对账；健康行可见；无外部 xtquant 调用；
- 任何阶段未过闸门不得进入下一阶段；100 万不得提前。

## 5. 未确认项（继续问券商）

1. 内置模型是否支持标准 `open()` 读本地 CSV/JSON？还是只能用
   `load_stk_list`/`load_stk_vol_list` 篮子格式？
2. 模型交易是否需要单独权限？与当前“要开权限”是同一项吗？
3. QMT 模型能否开机自启/无人值守常驻？
4. 云 Windows 是否算本地（是，但需书面确认）。
5. 模型内 Python 3.6 可用的第三方库清单（numpy/pandas？）。

**结论**：这不是“切个开关”，而是把执行/风控/对账链重做一遍；在 P2
通过前，100 万和所有外部 QMT 下单保持冻结。
