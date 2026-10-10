# 10-12 预演检查清单（2026-10-10 写死，12 日不当场想判据）

> 与 monday_20260914_checklist.md 同法：每条判据都是数字/命令，不留给当天叙事。
> 全部条款=runbook 已锁定条款的实现，无新增条款（冻结协议内）。

## 前置状态（2026-10-10 已核，机器值）

- 快照现态：35,772 行，max snap_date=2026-10-01，sha256=a5e779402f69…
- 工具：scripts/rehearsal_baseline.py（06:25 cron 用）/ scripts/rehearsal_check.py（14:00 用）
- 06:25 cron 安装命令已在账本十六轮节（用户 `!` 执行）；未装 → 预演缺"前"侧

## 09:00 前核对（两条，缺一即停上报）

```
(a) logs/cb_snapshots_baseline_2026-10-12.parquet 与 .json 存在（06:25 cron 产物）
    不存在 → 停，预演缺"前"侧，上报用户裁定（不可现场用别的文件顶替）
(b) .json 里 sha256 与 10-10 记录的 a5e77940… 一致（重建前旧文件）
    不一致 → 停，上报（06:25 拷错文件/重建提前跑了）
```

## 14:00 核验（一条命令六段，机器输出）

```
python scripts/rehearsal_check.py --date 2026-10-12 \
  --baseline logs/cb_snapshots_baseline_2026-10-12.parquet \
  --save-baseline-as 2026-10-19
```

| 段 | 判据 | 预期（10-10 预载） | FAIL 处置 |
|---|---|---|---|
| 1 重建证明 | mtime=当日06:30后+完成行+无ERROR | 全部满足 | 停，升级 |
| 2 行数判据 | 净增量∈[−100,600] | ≈0（月中不落新月份快照，11-02 才落） | 越界→停，升级 |
| 3 毛/净分报 | 新增/删除/净分开报 | 增删都小；评级/数据修正驱动 | 记入账本 |
| 4 字段级 | rating/价格改写数 | rating 改写若干（akshare 周刷新），价格改写少数（dblow 重推导） | 记入账本+漂移素材 |
| 5 两侧 PIT | 两侧全 PASS | 删除侧=锚定日无 bar；新增侧=锚定日有 bar+日期上限 0 违规 | 任一侧 FAIL→上报 |
| 6 漂移素材 | 模板打印 | monthly_zero 待 sched_compare 复跑后填 | — |

**⑤ 两侧 PASS → 全链重跑该项前置（评级字段时点化之外的快照侧）正式解除**；
毛变动=0 → 按"删除侧已清、新增侧未检"放行并在重跑记录写明范围（runbook 已锁）。

## 14:00 后（顺序固定）

1. `timeout 540 nice -n 10 ionice -c2 -n7 python3 scripts/backtest_cb_sched_compare.py`
   → 取 monthly_zero 填漂移日志第 5 行（预期 Δ 极小，10-05 实测 +0.001）
2. 漂移日志第 5 行（logs/cb_sharpe_drift_log.md）：行级（+X/−Y/净Z）与
   字段级（rating 改写 R / 价格改写 P）分开报，历史改写逐月分布入账
3. 记账（账本追加）：10-12 重建后实测行数 = 10-19 行数判据基线；
   实测新增/删除/净 → 定毛口径阈值、收紧净增量下限
4. 确认 logs/cb_snapshots_baseline_2026-10-19.parquet 已由
   --save-baseline-as 生成（10-19 的 diff 对端）

## 预演当日禁止

- 不涉及交易、不置锁、不动任何执行路径代码
- 预演暴露的问题只记录（10-16 定稿前按冻结协议修正 runbook，每处写明
  由哪条预演结果引起）
