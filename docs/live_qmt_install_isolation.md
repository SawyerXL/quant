# 国金实盘 QMT 安装 + 与仿真隔离（2026-11-01）

> 目标：同一台 Windows 上同时具备「仿真 QMT」和「实盘 QMT」，账户、安装目录、
> `userdata_mini`、`.env`、仓库、日志、信号、锁、计划任务全部隔离。
> **先装、先 dry-run，不下单；100 万切换仍冻结。**

## 1. 安装（Windows 上人工做）

1. 向国金客户经理申请 **实盘 QMT 权限/安装包**；确认实盘资金账号、交易权限、
   风险测评/协议已完成。仿真权限问题与实盘权限是两件事。
2. 安装到**新的 ASCII 目录**，例如 `D:\GJQMT`，不要覆盖现有仿真安装
   （当前仿真在 `H:\BaiduNetdiskDownload\...\国金QMT模拟环境...`）。
   避免中文/空格路径，减少编码/xtquant 解析问题。
3. 启动实盘终端，用**实盘账号**登录；确认其 `userdata_mini` 路径，例如
   `D:\GJQMT\userdata_mini`。记下实盘 `QMT_ACCOUNT_ID`。
4. 不要改现有 `H:\quant\.env`；不要用仿真账号登录实盘终端。
5. 若两个终端同时运行出现端口/单实例冲突，先**只开实盘**；验证后再试并行。
   监控 Windows 内存；该机曾出现内存压力。

## 2. 隔离矩阵（必须全部不同）

| 维度 | 仿真 | 实盘 |
|---|---|---|
| 安装目录 | 现有 UAT/模拟目录 | `D:\GJQMT`（新） |
| userdata_mini | 仿真路径 | `D:\GJQMT\userdata_mini` |
| 账号 | 仿真账号 | 实盘账号 |
| Windows 仓库 | `H:\quant` | `H:\quant_live`（或 `D:\quant_live`，新 clone，固定 tag） |
| `.env` | `ENV=simulation` | `ENV=production` + 实盘 `QMT_PATH/QMT_ACCOUNT_ID` |
| Linux 仓库 | `/root/quant` | `/root/quant_live`（新 checkout） |
| 信号/持仓 | `/root/quant/data_store/meta`、`logs/qmt_positions_latest.json` | `/root/quant_live/...` 独立 |
| 订单/结果/锁 | `H:\quant\logs` + `/root/quant/logs` | `H:\quant_live\logs` + `/root/quant_live/logs` |
| 计划任务 | `QuantTrackA_Daily` 等 | 新建 `QuantLive_*`，不与仿真共用 |
| 健康行/告警 | 现有 18:40 | 独立 live 健康行/告警（否则实盘故障不可见） |

## 3. 当前代码的硬编码（true isolation 的阻塞项）

只改 `.env` **不够**；以下路径会串到仿真：

- `scripts/qmt_snapshot.py:24`：`WIN_FILE = "H:/quant/logs/qmt_positions_latest.json"`
- `scripts/qmt_snapshot.py:41`：`H:\\quant\\scripts\\export_qmt_positions.py`
- `scripts/qmt_snapshot.py:78`：`H:/quant/scripts/fetch_and_execute.py --backfill`
- `scripts/fetch_and_execute.py:34`：`LINUX_SERVER`；`:48` 结果 scp 到
  `/root/quant/logs/...`
- `scripts/fetch_and_execute.py:72/78/123/131`：信号固定
  `data_store/meta/signal_*`，从 `/root/quant/...` 拉取
- `scripts/daily_signal_a_v2.py:157`、`daily_signal_a_grouped.py:106`：读
  `logs/qmt_positions_latest.json` 校正持仓
- `scripts/daily_health_line.py`：监控 `/root/quant` 的日志/快照/信号
- `data_store` 是共享符号链接；`data_store/meta` 的信号文件会被两个账户共用

**后果**：如果直接在 `H:\quant` 改 `.env` 为 production，实盘会：拉仿真的信号；
把实盘结果 scp 覆写仿真日志；用仿真持仓校正实盘信号；健康行看不到实盘故障。
**禁止这样跑。**

## 4. 推荐分阶段（代码改动必须带测试）

### Phase 0：安装 + 只读验证（不改代码、不下单）

- 安装实盘 QMT，登录实盘账号。
- 用实盘 `QMT_PATH` 跑 `qmt_conn_probe.py`（只读连接）。
- 用实盘 `QMT_PATH` 跑 `buy_002028_fix.py` dry-run（只读；若实盘无该券会
  提示无需修复；**不要 --go**）。
- 截图/记录 `userdata_mini` 路径、账号后四位、登录状态。

### Phase 1：profile 层（代码）

新增环境变量/配置，默认值保持仿真行为不变：

```
ACCOUNT_PROFILE=sim|live
QMT_PATH=...
QMT_ACCOUNT_ID=...
WIN_ROOT=H:/quant            # live: H:/quant_live
LINUX_REMOTE_ROOT=/root/quant # live: /root/quant_live
SIGNAL_DIR=data_store/meta    # live: /root/quant_live/data_store/meta
LOG_DIR=logs                  # live 独立
```

需要参数化/测试的文件：
`qmt_snapshot.py`、`fetch_and_execute.py`、`daily_signal_a_v2.py`、
`daily_signal_a_grouped.py`、`daily_health_line.py`、CB 信号输出路径。
测试至少覆盖：路径构造、profile 默认值、结果 scp 目标、信号目录、
健康行 profile 读取；不得改变 `ACCOUNT_PROFILE=sim` 的现有行为。

### Phase 2：实盘 dry-run

- 在 `H:\quant_live` 用 live `.env` 跑 `fetch_and_execute --plan-only`/
  dry-run（若支持），确认它拉的是 `/root/quant_live` 信号、写的是
  `H:\quant_live\logs`、连的是实盘 `QMT_PATH`。
- 跑 `qmt_snapshot.py`（live profile）确认快照落到 `/root/quant_live/logs`，
  不覆盖仿真。
- 跑 `pytest tests` 全绿。

### Phase 3：小资金实盘 pilot

- 停用/禁用仿真的**下单能力**任务（`QuantTrackA_Daily`、14:45 补跑、
  止损/T0 若会下单），只留研究/快照；或明确只跑一条执行链。
- 实盘先用小资金、单组、tier 流程；全部过 `execution/risk.py`；
  15% 熔断、单票 5%、单笔 10 万不变。
- 独立 live 健康行/告警；每天对账；3–6 个月后再谈 100 万。

## 5. 绝对不要做

- 不要覆盖现有仿真安装目录。
- 不要在 `H:\quant` 直接把 `.env` 改成 production 跑实盘。
- 不要共用账号、`.env`、`logs`、`qmt_positions_latest.json`、锁文件。
- 不要让实盘信号用仿真持仓校正（会买错/卖错）。
- 不要在没 dry-run/没测试前合并 `capital-live100w` 或切 100 万。
- 不要同时让两条执行链对同一信号自动下单；先一条链。

## 6. 需要用户/券商完成

1. 实盘 QMT 安装包与权限（国金）。
2. 实盘账号登录 + 资金到账。
3. 确认是否允许同一台机器并行两个 QMT 终端（端口/单实例）。
4. 确认实盘佣金/权限/风控参数。
5. 明确 live pilot 的资金规模与开始日期。

## 7. 当前实盘安装状态（2026-11-01）

- 用户已安装到 **`F:\国金证券QMT交易端`**；`XtItClient.exe` 已运行。
- `XtMiniQmt.exe` 存在于 `F:\国金证券QMT交易端\bin.x64\`，但**未运行**；
  根目录只有 `userdata`，**没有 `userdata_mini`**。
- 用 `userdata` 路径做只读连接：**QMT_CONN_FAIL result=-1**。
- 账号目录：`userdata\users\3994****`（这是登录用户目录，**基金账号需用户确认**；
  `accountInfo.ini` 是加密/二进制，未读取）。
- 结论：**主客户端已开，但 API 用的极简终端/`userdata_mini` 还没起来**。
  需在 QMT 登录界面选择**极简模式**（或启动 `bin.x64\XtMiniQmt.exe`）并用
  实盘账号登录，生成 `userdata_mini`；然后才能用 `QMT_PATH=...\userdata_mini`
  做只读 probe。
- 未改 `H:\quant\.env`；未下任何单。

## 8. 参考

- `docs/capital_switch_100w_checklist.md`（100 万切换前置，仍冻结）
- `docs/1102_qmt_down_contingency.md`
- `CLAUDE.md` 账户分离/风控红线
