# US 腿 5% 单票红线 — 实现路径（2026-10-21）

## 约束事实

- `execution/risk.py:117-129` `_check_single_position`：
  `after / self.state["total_assets"] > max_single_position` → 拦截。
- `trader._build_gateway(strategy_id, capital)` 把
  `total_assets` 设为**该 track 的信号 capital**（不是全账户）。
- 40/60 股票 track capital=**64 万**，`max_single_position=0.05`
  → 单票上限 **3.2 万**。
- US 12.5% = **20 万**：单只 20 万=31%；拆 3 只各 6.7 万=10.4%；
  拆 7 只各 2.9 万=4.5% 才过线——**拆分不可行**（A 股宽基 QDII 只有
  3~4 只可用，且 7 只纯冗余）。

## 可行路径

### A（推荐）独立 US track/账户

- `strategy_id=track_us`，独立 `risk` 配置：
  - `total_assets` = US 腿资金（20 万），`max_single_position=1.0`；或
  - `total_assets` = 核心总资金（160 万），`max_single_position=0.15`。
- 独立台账/NAV；与股票/CB 腿的资金、风控、对账隔离（同卫星章程的
  "独立账户"原则）。
- 需要：
  1. US 信号文件（目标权重/代码/张数；固定再平衡，如季度或年）；
  2. `trader._in_scope`/positions 识别 `track_us` 的 ETF 持仓；
  3. 执行映射（QMT 代码/交易所/tick/lot；513650/159655/159612 或 513110）；
  4. 监控（溢价 IOPV≤5%、FX、NAV 分腿）；
  5. 与核心 15% 线的关系：US 腿不进入股票腿的 15% 熔断分母
     （或按用户裁定合并口径）。
- 这是**一个独立项目**，不是快速改动；建议在溢价 ≤5% 触发后再建，
  避免闲置基础设施。

### B 备选 per-code 风险豁免（不推荐）

- 在 `risk.py` 为 US ETF 加 per-code override（如 0.15）。
- 问题：US 20 万仍计入股票 track 的 `total_assets=64万` →
  现金 `capital−mv_total` 与 NAV 被扰动；`_scoped_positions` 的
  股票清仓/MA10 逻辑可能误碰 ETF。需要额外守卫 + 测试。

## 与 10-20 方案的关系

10-20 的 "12.5% 起步、独立记账" 已隐含独立台账；但未说明 5% 红线
的分母。**本次澄清：独立 track/账户是唯一干净路径**。溢价 ≤5% +
risk 路径定案后，才按 12.5% 起步（用户确认 + 落地对账）。
