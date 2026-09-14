# 回滚 patch 存档（2026-09-13 晚，机器时间）

**背景**：五点反馈第二处置的四脚本改动，因验证命令（py_compile）在会话中
被拒三次，按用户裁定执行回滚分支——"未验证代码不在无人值守 cron 里首跑，
跑不了验证就回滚，改动存 patch，周二盘后有人盯着时再上"。

**周二重上流程**：逐项 apply 下列 old→new 对 → py_compile 四文件 → 健康行
手动复跑一次（应零告警）→ p0 sweep --dry-run → qmt_snapshot
QMT_SNAPSHOT_MAX_ATTEMPTS=1 单次验证 → 确认 spool/master 同步 → 提交。

---

## 1. scripts/p0_alerts.py（sweep 时点 19:10 + 判据 ts≠今日）

old:
```
    python scripts/p0_alerts.py --sweep               # 升级清扫(cron 18:10)
```
new:
```
    python scripts/p0_alerts.py --sweep               # 升级清扫(cron 19:10, 2026-09-13晚由18:10移后: 必须读当日健康行)
```

old:
```
HEALTH_LINE_BOOTSTRAP_UNTIL = "2026-09-14 19:00"
```
new:
```
HEALTH_LINE_BOOTSTRAP_UNTIL = "2026-09-14 19:15"
```

old（_check_health_line 判据）:
```
    try:
        ts = json.loads(hp.read_text(encoding="utf-8")).get("ts", "")
        age_h = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                 ).total_seconds() / 3600
    except Exception:
        age_h = 999.0
    if age_h > 26:
        if not dry_run:
            send_alert(f"[P0] 健康行 {age_h:.0f}h 未更新(>26h), "
                       f"机器或 cron 静默死亡", level="error")
        logger.warning(f"健康行 {age_h:.0f}h 未更新{'[dry-run]' if dry_run else ''}")
```
new:
```
    try:
        ts = json.loads(hp.read_text(encoding="utf-8")).get("ts", "")
        ts_date = ts[:10]
    except Exception:
        ts_date = ""
    if ts_date != now_s[:10]:
        if not dry_run:
            send_alert(f"[P0] 健康行时间戳={ts_date or '不可读'}≠今日"
                       f"{now_s[:10]}, 机器或 cron 静默死亡", level="error")
        logger.warning(f"健康行 ts={ts_date}≠今日{'[dry-run]' if dry_run else ''}")
```
+ docstring 补："2026-09-13晚用户裁定改判据: sweep 移到健康行之后(19:10),
健康行时间戳应当是今天, 不是'<26h'——'<26h'在 18:10 读昨天的行只有 2.6h
余量且当天故障当天不可见。"

## 2. scripts/qmt_snapshot.py（重试窗口 + 状态落盘 + 环境旋钮）

- 模块 docstring 增加重试窗口说明段（2026-09-13晚用户裁定…）
- `import sys, json, subprocess, ...` → 增加 `os`；增加 `import time`
- 常量区增加：
```
STATE_FILE = Path("logs/qmt_snapshot_state.json")
# 重试窗口(2026-09-13晚): 15:05 起每 10 分钟一次, 至 15:55 共 6 次。
# 手动验证可用 QMT_SNAPSHOT_MAX_ATTEMPTS=1 缩短; 缩短窗口的全败不发 P0
# (P0 语义=收盘后仍全败, 缩短窗口不构成收盘判定)
RETRY_INTERVAL_S = 600
MAX_ATTEMPTS = int(os.environ.get("QMT_SNAPSHOT_MAX_ATTEMPTS", "6"))
```
- run() 重写为：重试循环（10min×MAX_ATTEMPTS）→ 状态落盘 STATE_FILE
  （date/attempts/ok/last_error/ts）→ 全败时 `if attempt >= 6` 才 p0_alert
  → 成功才 update_nav/backfill/pull_missing。

## 3. scripts/daily_health_line.py（snapshot_state 字段 + 交易日全败告警）

- 增加块：读 logs/qmt_snapshot_state.json → snap_state（异常→
  {"error": "状态文件不可读"}）
- alerts 增加条件：
```
    if snap_state.get("date") == now[:10] and not snap_state.get("ok"):
        alerts.append(f"今日快照 {snap_state.get('attempts', '?')} 次尝试全败, "
                      f"持仓不可观测")
```
- line dict 增加 `"snapshot_state": snap_state,`

## 4. scripts/backtest_engine.py（fallback 横截面中位数）

old（inv_vol 分支）:
```
                    vols = {}
                    for c in new_w:
                        rr = panel[c].iloc[max(0, i-21):i].pct_change().dropna()
                        if len(rr) >= 10:
                            v = float(rr.iloc[-20:].std() * np.sqrt(252))
                            vols[c] = min(1.0 / max(v, 0.05), 3.0)
                    if vols:
                        tot = sum(vols.values())
                        w_sum = sum(new_w.values())
                        new_w = {c: (vols.get(c, 1.0) / tot) * w_sum
                                 for c in new_w}
```
new:
```
                    vols, vs = {}, []
                    for c in new_w:
                        rr = panel[c].iloc[max(0, i-21):i].pct_change().dropna()
                        if len(rr) >= 10:
                            v = float(rr.iloc[-20:].std() * np.sqrt(252))
                            vols[c] = min(1.0 / max(v, 0.05), 3.0)
                            vs.append(v)
                    if vols:
                        # 2026-09-13晚用户裁定: fallback 从常数 1.0(静默减配到
                        # 典型权重的30%)改当日横截面 vol 中位数对应因子——
                        # 缺失样本获得中性权重, 不引入方向性倾斜(否则 D2−基准
                        # 差分混入"fallback普及度随时间变化"成分); 分母纳入
                        # 缺失票, 顺带修掉旧实现权重之和超 w_sum 的重叠
                        med_v = float(np.median(vs)) if vs else 0.05
                        fb = min(1.0 / max(med_v, 0.05), 3.0)
                        factors = {c: vols.get(c, fb) for c in new_w}
                        tot = sum(factors.values())
                        w_sum = sum(new_w.values())
                        new_w = {c: (factors[c] / tot) * w_sum for c in new_w}
```
- 注释行同步：503-509 两处硬化说明 + 样本不足行改为中位数语义。
- **gate 声明（2026-09-13晚查证）**：此修复在 `if weight_scheme == "inv_vol"`
  分支内，DEFAULT=equal 不受影响——只影响 D2，不碰共享归一化路径；
  但周二复核若引擎已含此修复，效应值须带第四元（引擎版本）。

## 5. cron（spool + master /tmp/quantcron.txt）

- sweep: `10 18 * * 1-5` → `10 19 * * *`（回滚后恢复 18:10）
- 快照: `12 15 * * 1-5` → `5 15 * * 1-5`（回滚后恢复 15:12）
- 健康行注释: "p0 sweep 18:10 检查 >26h" → "p0 sweep 19:10 检查 ts≠今日"

## 6. 文档同步（回滚后已同步恢复）

- 登记文档 §7：样本不足行（1.0 减配 → 横截面中位数）+ "两处硬化"→"三处硬化"
- directions JSON D2 note：增加 fallback 中位数字句
