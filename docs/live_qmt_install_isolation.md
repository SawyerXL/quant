# 国金实盘 QMT 安装 + 与仿真隔离（2026-11-01）

> 目标：同一台 Windows 上同时具备「仿真 QMT」和「实盘 QMT」，账户、安装目录、
> `userdata_mini`、`.env`、仓库、日志、信号、锁、计划任务全部隔离。
> **先装、先 dry-run，不下单；100 万切换仍冻结。**

## 0. 2026-11-01 券商约束：QMT“本地编写、本地运行”

券商反馈 QMT **只支持本地编写、本地运行**。这不必然推翻当前架构，但必须先确认
“本地”的边界：

| 解释 | 对当前架构的影响 |
|---|---|
| A. 同机外部 Python 可调用 `xtquant/XtQuantTrader` | 兼容：`fetch_and_execute.py` 本就在 Windows 本地跑；Linux 只生成信号文件。若禁止 SSH 远程触发，改用 Windows 本地 Task Scheduler |
| B. 只允许 QMT 内置 Python 编辑器 | 不兼容：`fetch_and_execute`/`cash_sweep`/`qmt_snapshot`/健康行都要移植到 QMT 内置策略；100 万切换重新排期 |
| C. 外部 Python 可运行，但代码必须在本地 Windows 编辑/部署 | 兼容但流程要改：Linux 不直接改/执行 live 代码；用 Windows 本地仓 + Git/同步，Linux 只产信号 |

**必须向券商书面确认**：
1. 同机外部 Python 的 `xtquant`/`XtQuantTrader` API 是否开放？
2. 是否允许 Windows 计划任务/本地脚本启动，禁止 SSH 远程触发？
3. Linux 只生成信号文件、通过共享目录/scp 传到 Windows 本地，是否允许？
4. Windows 云服务器（当前这台）是否算“本地”？
5. miniQMT/极简模式/独立交易/API 权限如何开通？

确认前**不改架构、不下单、不切 100 万**；当前仍 `result=-1`、002028 −399。

### 0.1 本机 QMT 帮助文档核对（2026-11-01）

已读取 `F:\国金证券QMT交易端\config\help`：

- `python\迅投QMT极速策略交易系统_模型教程_Python_API_说明文档_Python3.pdf`
  （171 页，doc 3.3.6 / Python 3.6.8 / 2021.12.20）。
- 文档明确：QMT 支持 **VBA / Python 内置模型**；模型在 QMT 客户端内
  `init(ContextInfo)` → `handlebar(ContextInfo)` 逐 K 线运行，点击【运行】
  启动；下单用 `passorder`、查持仓/委托用 `get_trade_detail_data`、
  定时器用 `run_time`，可读本地篮子文件（`load_stk_list`/
  `load_stk_vol_list`）。
- 该文档 **没有出现 `xtquant`/`XtQuantTrader`**；官方文档路径是内置
  Python 模型，不是外部 Python 调用。因此：
  - 若券商确认“本地”=QMT 内置模型，则 **现产 `fetch_and_execute + xtquant`
    不是可交付路径**，执行层必须移植为 QMT 内置模型；
  - Linux 仍可做研究/信号，但要把信号落成本地 CSV/篮子文件给内置模型读；
  - 快照/健康/对账需由内置模型写本地文件，或另找官方允许的本地进程。
- 仍待券商确认：同机外部 Python 的 xtquant API 是否另外开放；内置模型
  是否允许标准 `open()` 读 JSON（文档只保证篮子 txt/csv）。
- 结论：**先按内置模型做 port spike（只读/模拟，不下单）**，不把现产
  xtquant 链当默认；100 万继续冻结。

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

### Phase 1：profile 层（2026-11-01 已实装，默认 sim 不变）

已加环境变量：
- `fetch_and_execute.py`：`ACCOUNT_PROFILE`、`LINUX_REMOTE_ROOT`、
  `LOCAL_SIGNAL_DIR`、`REMOTE_SIGNAL_DIR`；结果 scp 与信号拉取都走 remote root。
- `qmt_snapshot.py` / `qmt_health_alert.py` / `measure_execution_costs.py`：
  `WIN_ROOT`（默认 `H:/quant`；live 设 `H:/quant_live`）。
- `cb_signal.py` / `daily_signal_a_v2.py` / `daily_signal_a_grouped.py`：
  `SIGNAL_DIR`（默认 `data_store/meta`；live 设 `signals`）。
- 新增 5 个 profile 测试；全量 **257 passed**；默认仿真路径完全不变。

### Phase 1.1：live `.env` 模板（不要提交）

Windows `H:\quant_live\.env`：
```dotenv
ENV=production
ACCOUNT_PROFILE=live
QMT_PATH=F:/国金证券QMT交易端/userdata_mini
QMT_ACCOUNT_ID=<实盘资金账号>
LINUX_SERVER=106.15.61.81
LINUX_PORT=12201
LINUX_REMOTE_ROOT=/root/quant_live
REMOTE_SIGNAL_DIR=signals
LOCAL_SIGNAL_DIR=data_store/meta
```

Linux `/root/quant_live/.env`：
```dotenv
ACCOUNT_PROFILE=live
SIGNAL_DIR=signals
WIN_ROOT=H:/quant_live
```

要点：Windows 仓 `H:\quant_live` 与 Linux 仓 `/root/quant_live` 各自独立；
信号走 `signals/`，不碰仿真 `data_store/meta` 的 signal 文件；
`data_store` 行情数据可共享，但 `meta/signal_*` 必须分开。

### Phase 1.2：尚未覆盖

- `daily_health_line.py` 仍按仓库相对路径运行；live 需在 `/root/quant_live`
  跑独立健康行（或后续加 profile），否则实盘故障不可见。
- `LOG_DIR` 未全局参数化；靠独立仓库/目录隔离（`/root/quant_live`,
  `H:\quant_live`）。
- `run_daily.bat` 仍按 Windows 仓内相对路径；live 仓需单独任务/入口。

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
- 用户反馈（2026-11-01）：登录界面勾选“独立交易”时密码不对，不勾选可正常登录。
  “独立交易”很可能使用**交易密码/资金密码**（与登录密码不同），需券商确认/
  重置；**不要连续试错，避免账号锁定**。先找“极简模式”入口（不一定是
  “独立交易”），用正常登录密码进入 mini；若 mini 也要求独立交易，再向
  国金确认交易密码。目标仍是生成 `userdata_mini`，不是强行勾选。
- **最新（2026-11-01）**：启动 mini/独立交易时提示**需要开通权限**。
  说明实盘账号的 **QMT 极简交易/独立交易/API 权限未开通**；主客户端
  正常登录≠mini/API 权限。需国金客户经理开通（可能要协议/资产/风评）；
  开通前不要再试密码，live 不执行、不下单。

## 8. 参考

- `docs/capital_switch_100w_checklist.md`（100 万切换前置，仍冻结）
- `docs/1102_qmt_down_contingency.md`
- `CLAUDE.md` 账户分离/风控红线
