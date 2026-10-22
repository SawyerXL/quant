# US 腿 track 设计 — 决策备忘（2026-10-22）

> 前提：DJIA/513400 已过 N=72 五条判据（`logs/us_leg_dji_verdict_20261022.md`）；
> 实时 IOPV 源已打通（Tencent 77/78；513400 现价 1.321 / IOPV 1.2923 /
> 溢价 2.30%，10-22 09:50）。**唯一剩余阻塞 = 5% 单票红线 / 独立 track。**

## 1. 需要用户拍板的三个选项

| 选项 | 内容 | 风险口径 | 代价 |
|---|---|---|---|
| **A（推荐）独立 `track_us`/账户** | US 资金 20 万（12.5%×160 万）单独 `total_assets`、单独台账/NAV；`max_single_position=1.0`（单 ETF 513400）；订单拆 ≤10 万/笔 | 核心 15% 线**不含** US（或按用户裁定合并）；US sleeve 无单独 15% 熔断 | 需新建信号/执行/对账/监控链路；账户/券商由用户定 |
| B 修订 Track A 单票红线 | 在股票 track 内给 US ETF per-code 豁免 | 20 万/64 万=31%，仍超线；需改风控红线+测试 | 不推荐：ETF 混入股票 track 会扰动现金/NAV/MA10 清仓逻辑 |
| C 先只监控不建 track | 513400 继续 watch-only；等溢价/架构自然成熟 | 不下单，无风险 | 错失低溢价窗口；但可等 |

**推荐 A**；若选 A，用户只需确认：①US sleeve 的 15% 熔断口径（建议
**不设 sleeve 级 15%**，由核心账户级 15% 覆盖；sleeve 独立回撤 −37% 会在
2020/2022 冻结）；②账户安排（独立 QMT 账户或同一账户的独立 track 命名空间）。

## 2. 执行机制（A 的落地细节）

- **信号**：DJIA T-1 收盘 → 中国 T 日 14:45；目标 US 权重 12.5%（200k）。
- **载体**：513400（sh，tick **0.001**、lot 100）。
- **单笔上限**：`MAX_SINGLE_ORDER_VALUE=100,000` → 200k 拆 **3 笔**
  （约 6.6/6.7/6.7 万），每笔单票占比 = 6.7万/20万 = 33.5% < track
  `max_single_position=1.0`，过 `risk.py`。
- **入场闸门**：14:45 实时 Tencent IOPV 溢价 **≤5%** 才买；>5% 保持现金
  （现金可吃 1.4% 货币收益）。卖出不受溢价闸门限制（高溢价卖出反而有利）。
- **再平衡**：**不要按股票双周频率**重平衡 US sleeve——回测的 3bp/边不含
  QDII 溢价；实际每次买卖的溢价是 2~5% 量级。建议 **季度 + ±3pp 带宽**，
  只在漂移越界时交易边际份额；初始建仓一次 2.3% 溢价 ≈ 0.29% 核心
  一次性成本（7.8 年摊薄 ~0.04pp/年）。
- **溢价成本敏感**：若按双周重平衡全额来回，2.3%×2×12 ≈ 55% sleeve/年，
  会吃光 +2.43pp；带宽再平衡后年成本约 0.05~0.15pp 核心。**这一条必须在
  实盘前用实际成交校准**。
- **口径不同构（重要）**：回测 +2.43pp 是**双周回权重、3bp/边、不含溢价
  交易成本**；本设计建议季度+±3pp 带宽。二者未同构，**不得把 +2.43pp
  直接当部署期望**；实盘前要用真实溢价成本重算后再登记预期。
- **币种/日历**：DJIA 美元、513400 人民币价（含汇率）；US 假日/A 股日历
  交集；与 S&P/IXIC 腿同病，已披露。
- **监控**：18:30 `qdii_premium_monitor.py`（实时 IOPV + T-1 NAV 兜底）；
  健康行 `watch_hits`/`rt_premium_pct`；US track 独立 NAV 日更。

## 3. 风险路径（供 15% 线裁定）

- DJIA 指数 2019-2026 价格最大回撤 **−37.1%**（2020）、2022 **−21.9%**；
  513400 价格 2024-02 起 **−20.5%**。
- 12.5% 权重下 sleeve 对核心的裸贡献约 **−4.6pp**（−37.1%×12.5%）。
- 组合回测（含 US）2019-2026 最大回撤 **−13.49%**，15% 熔断 **0 触发**
  （CB 腿的分散化在历史上吸收了 US 冲击）。
- 因此：**不建议给 US sleeve 单独 15% 熔断**；若 US 独立账户，需用户定义
  该账户自己的回撤规则（否则 sleeve 会在 2020/2022 被冻结而错过修复）。
  核心账户级 15% 线继续按现有定义执行。

## 4. 未决/下一步

1. 用户拍板 A/B/C（见 §1）。
2. 若 A：先做**仿真链路**（US 信号文件 → 14:45 限价单 → tick 归一 → 成交
   对账 → 独立 NAV），不下真钱；与 CB 月度/股票月末错开。
3. 实盘前：513400 溢价连续 ≤5% 的窗口 + 10 万拆单测试 + 15% 线口径确认。
4. 若 B：需修订风控红线与 `execution/risk.py`，并重跑所有含 US 的组合
   回测；不推荐。
5. 若 C：维持 watch-only，监控每日记录；等溢价/架构成熟再启动。

## 5. 工件

预注册 `logs/prereg_us_leg_dji_20261022.md`；结果
`logs/us_leg_dji_20261022.json`；结论 `logs/us_leg_dji_verdict_20261022.md`；
脚本 `scripts/us_leg_dji_sim.py`；监控 `scripts/qdii_premium_monitor.py`。

## 6. 已落地（2026-10-22，用户选 A）

- `config/strategy_params/us_track.py`：20万、单ETF 100%、sleeve 无熔断、
  溢价阈值5%、3pp带宽、14:45。
- `scripts/us_track.py`：`--init/--signal/--paper-fill/--mark`；
  - 10-22 10:00 dry-run：513400 现价 1.322 / IOPV 1.2923 / 溢价 **2.30%**、
    DJIA T-1=52856.09（10-21）；目标 20万 → 拆 **75,600+75,600 股**
    （各 99,943 元），风控 `track_us` 两条全过；信号
    `data_store/meta/signal_us_latest.json`。
  - 纸面状态 `logs/us_track_state.json` 已初始化 20 万；`--mark` 10-22
    nav=200,000（0 持仓，尚未到 14:45）；`logs/us_track_daily.csv` 已建。
  - 守卫：交易日历（14:45 当日 bar 未落库，不能查 bar）+ 买入信号
    as_of <14:40 拒绝纸面成交；单笔≤10万拆单；现金含费缓冲。
- `tests/test_us_track.py` 8 项 + `tests/test_risk.py` 36 项 = **44 passed**。
- **risk.py 已落盘（2026-10-22，用户全权授权后）**：`_STRATEGY_RISK["track_us"]`
  正式映射 + `_check_account_drawdown` 按 track 取 `max_drawdown_circuit`
  （track_us=1.0，sleeve 无熔断；track_a 0.15 不变）+ 缺失配置 fail-closed。
  `us_track.py` 的运行时注入与 `USRiskGateway` 子类已不再必要（保留子类
  作为显式红线，检查全部走 `execution/risk.py`）。**deny 已恢复**。
- Cron：三条已装入**实盘 crontab**（`crontab -l` 验证：14:45 信号 / 14:50
  纸面成交 / 18:35 mark，均含 `cd /root/quant`）；`deploy/` 记录同步。
- 测试：`tests/test_risk.py` + `tests/test_us_track.py` **44 passed**；
  `track_us` 映射实测 `{max_single:1.0, max_industry:1.0, max_drawdown:1.0}`。
- **仍未做的**：真钱执行（等账户/券商/Windows 侧接线）；US track 的
  独立 NAV 与核心 15% 线的合并口径（用户账户安排）；用真实溢价成交校准
  再平衡成本。
