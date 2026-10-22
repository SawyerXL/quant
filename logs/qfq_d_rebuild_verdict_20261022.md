# qfq D 分层重建 — 结案（2026-10-22）

> 承接 `logs/qfq_full_library_decision_20261021.md` 的 C+D 路径。D = 只重建
> 策略实际候选池（rebal60 并集）中 registry 非 qfq 的票；B（全库）暂缓。
> 本文件记录：执行、验收、策略影响、发现的两个数据边界问题、残余风险。

## 1. 范围与结果

- rebal60 并集 **882 只**；registry 非 qfq、非北交所、非 601059/601198 =
  **453 只**（`logs/qfq_d_universe_20261021.csv`）。
- **452 只重建成功；000016 跳过；0 失败**（`logs/rebuild_d_universe_20261021.json`）。
- 000016 = *ST康佳A，锚窗口（2026-10-01~10-21）无数据（最后有效 bar
  2026-09-03），不重建；registry 保持非 qfq。
- 方法：新浪 qfq 全史重拉 + 前沿锚（±0.5%）+ 备份（`data_store/backup_qfq_rebuild_20261021/`）
  + 按年覆写 + 源端 max 之后已有 bar 保留（尾部保留已修，见 §4）。

### 执行插曲（已修复）

- 首跑 18:32 启动，在 600559 后对某券的源请求**无限挂起**，被 2h timeout
  于 20:32 杀掉：已写 339 只，未写清单 JSON。
- 续跑 `--codes-file` 只处理缺失 114 只；新增**单券 150s SIGALRM** 硬超时
  （触发后由 FallbackDataSource 落 MCP 备源）→ 113 只成功、000016 跳过、
  0 失败；总耗时约 8 分钟。
- 教训入闸门十八：`except: pass` 的尾部保留分支原本是死代码，已改为
  `fp = dyr / f"{code}.parquet"` 并记录 `n_tail_kept`（本次 0）。

## 2. 验收（增强版，闸门十六）

`scripts/verify_qfq_d_rebuild.py`，25 只随机抽样（seed=20261021）：

| 检查 | 结果 |
|---|---|
| 新浪同源 4 探针（±0.5%） | **24 直接通过 + 1 anchor_shift 放行** |
| 独立源 baostock qfq（±1%） | **25/25 通过**（校准差 <0.5%） |
| 已知 raw 自检（000090） | 检出（0/1.08/15.44/24.65%）→ SELFTEST_PASS |
| 探针边界（闸门二十二） | 早于上市/停牌的探针记为 skipped，不判失败（已修） |
| verdict | **PASS** |

- **002250 anchor_shift**：四个探针一致偏 0.76%，独立源 baostock 比值
  ~0（0.0000~0.0002）→ 库内值正确，新浪源在写入后因新除权整体重锚。
  代码已编码该情形：同向、幅度一致（≤1%、极差≤0.2pp）且独立源通过才放行。
- 报告 `logs/verify_qfq_d_rebuild_20261021.json`。

## 3. Registry 与策略影响

- `build_adjust_registry.py` 重跑：**qfq 931→1383（+452）**，
  unknown_unaudited 4106→3665，raw 183→172，BSE 351、delisted_merger 2 不变。
- 40/60 三件套（`--cb-col nav_snapdelay_par`，2019-01~2026-10-19，n=1887）：

| 指标 | 修前 | D 修后 | Δ |
|---|---:|---:|---:|
| 组合年化 | 12.54% | **12.57%** | +0.03pp |
| 夏普 | 1.480 | **1.483** | +0.003 |
| 最大回撤 | −13.98% | **−13.98%** | 0 |
| 15% 熔断 | 0 触发 | 0 触发 | — |
| 股票腿 32万/组 | 1.50%/0.212/−16.66 | **1.58%/0.22/−16.61** | +0.08pp |

- 结论：修复属**数据口径纠正**，不改任何参数、不改配置；收益影响在噪声内，
  不据此调整部署期望。40/60 定案数字仍为 ~12.5% 量级（价格口径）。
- **加载器零价拒收后复跑（final2）与 final 完全一致**：12.57%/1.483/−13.98，
  股票腿 1.58%/0.22/−16.61——零价行不在选中池，符合预期。
- 工件（最终）：`logs/three_piece_40_60_d_final2_32w_20261021.json`；
  对照 `logs/three_piece_40_60_d_final_32w_20261021.json`。

## 4. 两个数据边界发现

### 4.1 零价占位行（已修）

- 000016 在 2026-09-24 有 `close=0.00 / volume=0` 的占位行；`load_daily`
  原样返回。相对跳变守卫用 `if prev_close and c`，c=0 为假值**放行**。
- 修复：`data/storage.py::load_daily` 统一拒收 `close<=0/NaN` 行并记
  warning；`tests/test_storage.py` 新增用例，4 passed。000016 读取后最后
  有效 bar=2026-09-03（2.46）。
- 影响：000016 不在选中池（amount=0），40/60 结果预计不变；但若未来某票
  零价行被持有，旧逻辑会算出 −100%，属闸门二十二同族。

### 4.2 新除权后的历史重锚（P1，未修）

- 日更只追加当日 bar；若之后发生除权/分红，源端 qfq 会把**全史**重锚，
  库内旧史仍是写入时口径 → 近端 vol20/MA10 跨除权日可能出现幻影跳变。
- 002250 是活例：D 写入后新除权使 fresh 新浪全史下移 0.76%，baostock 证明
  库内值在 as-of 时点正确。
- 建议：①每周/每日抽样"库内最后 bar vs 源端同日 qfq"的比值，出现同向
  整体偏移→触发该票重拉；或②改存 raw + adjust factor（根治，成本高）。
  在这之前，**近端信号仍需假定 qfq 自写入后未重锚**。

## 5. 盲区声明（闸门七）

- 25 只抽样、4 探针，不能证明 452 只全部无错；未抽样票依赖同源写入正确。
- baostock qfq 与新浪的因子取整差可到 ~0.5%，1% 容差会放过小于 1% 的
  真实口径差（但不会放过大类 raw/mixed 污染，量级 5%+）。
- 重建文件为 9 列（与 A 同款）：`load_daily` 本就丢弃 `量比/换手率/名称`
  等非契约列，策略只用 OHLCV+amount，故无消费方影响；但**文件层 schema
  不一致**（452+651 只 9 列，其余 19 列），新脚本若直接读 parquet 需容忍。
- 000016 未重建且为 *ST/停牌；其 raw 旧史仍可能被非策略研究误用。
- D 只覆盖 rebal60；rebal120 的 964 只非 qfq、全库 B 仍未做。
  新做小盘/全库宇宙研究前必须先判定 registry。

## 6. 产出

- `scripts/rebuild_d_universe_qfq.py`（续跑/超时/尾部保留）
- `scripts/verify_qfq_d_rebuild.py`（独立源/边界/anchor_shift/selftest）
- `scripts/build_adjust_registry.py`（qfq 1383）
- `data_store/backup_qfq_rebuild_20261021/`、`logs/rebuild_d_universe_*_20261021.json`
- `logs/verify_qfq_d_rebuild_20261021.json`、
  `logs/three_piece_40_60_d_final_32w_20261021.json`
- `logs/qfq_d_universe_20261021.csv`、`logs/d_missing_codes_20261021.txt`
