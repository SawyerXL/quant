# 新数据源接入检查清单（2026-10-22，吸收 TickFlow custom-data-source 思路）

现有链路：`data/source.py` 的 `DataSource` 基类 + `get_source()`（`config/settings.py` 选源）；
MCP 为主源、Akshare/新浪为兜底、本地 `data_store` 为契约存储。**新增源不改变生产契约，只替换取数实现。**

## 0. 准入原则

- 新源必须实现 `DataSource` 的既有方法语义（`get_daily`/`get_trade_calendar` 等），不得让调用方感知差异。
- **配置缺失 fail-fast**（闸门五）：源地址/路径/代码不得有 fallback 默认值。
- **禁止从数值反推元数据**（闸门十一）：每条记录由 ingestion 层打 `source`+`unit` 标签落盘。
- 新源先作为**备源**接入降级链，稳定 ≥1 周且过全部检查后才可提升主源。

## 1. 身份（identity）

- [ ] 6 位代码归一走 `data/code_norm.py`，不手写前缀规则
- [ ] 沪/深/北交所（含 920/430/83/87）后缀正确；指数键用 `SH/SZ` 前缀命名空间
- [ ] 同一标的不得出现两个键（闸门十五：迁移后扫僵尸键）
- [ ] 退市/改名/ST 不改变代码身份；停牌不等于删除

## 2. 单位（unit）

- [ ] 库契约：**close 元、volume 万股、amount 万元**
- [ ] 源端若为元/股，接入层必须 `/1e4`，并写 `unit` 标签
- [ ] 复现 `tests/unit_ground_truth.json`（MCP 5 票×3 日期）；复现不了不允许合入（闸门八）
- [ ] 锚校验遇 MCP 零额：跳过并计数（零值≠停牌）

## 3. 连续性 / 脏跳（continuity）

- [ ] 逐票相邻日 amount 比值 < 100；≥100 即报（10^4 跳变当日暴露）
- [ ] 日跳 >50% 的脏值扫描；命中先白名单取证再修
- [ ] close<=0 / NaN 拒收（`load_daily` 已实现）
- [ ] 时间常量禁止字面量（闸门十三）；相对表达式产物必须冻结 `as_of`

## 4. 量纲 / 绝对量级（scale）

- [ ] `amount / volume ≈ close`，比值 ∈ [0.5, 2]
- [ ] 单票单日成交额 < 1000 亿元（ETF 走单独比值审计，不套股票上限）
- [ ] 全市场合计 ∈ [3000 亿, 3 万亿] 元（闸门十七）
- [ ] 比值检查之外必须有绝对量级哨兵（双字段同倍污染比值失效）

## 5. 复权（adjust）

- [ ] 源端复权口径明确（raw/qfq）；入库写 `adjust_basis_registry`（qfq/raw/unknown）
- [ ] 前复权前沿锚：最新 bar 与库内偏差 ≤0.5%，否则在锚窗口内比对，不做全史 MAX
- [ ] 新除权重锚走 `rebuild_d_universe_qfq.py --reanchor-file`（公告确认+备份+验收），禁止自动重锚
- [ ] 回测引用前查 registry；unknown/raw 一律附“待复核-复权混存”声明

## 6. 新鲜度（freshness）

- [ ] 每交易日 17:00 后主源可取得当日 bar；T-1 滞后源必须显式降级
- [ ] 快照类数据记录导出时点；≥15:00 才可当收盘价用
- [ ] 健康行写 `latest_bar`/`bar_fresh`；禁止用 `max(date)` 当面板新鲜度（闸门十四）

## 7. 失败 / 降级（failover）

- [ ] “先试 A 失败再试 B”的分支必须**被执行过**（闸门十八），B 不能是死代码
- [ ] 跳过/降级/回退必须写健康行（闸门十二），不得静默返回
- [ ] 降级告警进 events/健康行；连续失败计数
- [ ] 超时硬约束（单票/单请求），避免整链挂起

## 8. 测试（tests）

- [ ] 单元测试覆盖：正常、空返回、单位错、脏跳、零价
- [ ] 正控用例：已知应失败的数据必须被判失败（闸门十六）
- [ ] 下游消费者视角检查：TOP30 排名/信号预览/回测冒烟（闸门十一尾部队列）
- [ ] `preopen` 冒烟通过后才允许 cron 切换

## 9. 最小适配器示例（只示意，不直接合入）

```python
from data.source import DataSource

class MySource(DataSource):
    name = "my_source"

    def __init__(self, base_url: str):
        if not base_url:                      # 闸门五：配置缺失 fail-fast
            raise ValueError("MySource base_url 未配置")
        self.base_url = base_url

    def get_daily(self, code: str, start: str, end: str):
        raw = self._fetch(code, start, end)   # 返回源端原始记录
        df = self._normalize(raw)             # code_norm + 单位 /1e4 + close<=0 拒收
        df.attrs["source"] = self.name
        df.attrs["unit"] = {"close": "元", "volume": "万股", "amount": "万元"}
        return df
```

## 10. 接入步骤

1. 在 `data/my_source.py` 实现子类（不改 `DataSource` 接口）；
2. `config/settings.py` 增加源选择与凭证（只读 `.env`）；
3. 跑 `tests/unit_ground_truth` 锚 + 本清单第 2–7 项检查；
4. 作为备源挂进降级链，观察 ≥1 周健康行 events；
5. 提升主源前跑一次 `preopen` 与下游 TOP30 对比，留 `as_of` 记录。
