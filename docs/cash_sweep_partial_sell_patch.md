# cash_sweep 部分卖出补丁（2026-10-27 待审，不改代码）

> 对象：`scripts/cash_sweep.py`。当前该文件写权限被拒，本文件只记录
> 已复现的缺陷与最小修复方案；未修改任何生产代码。
> 触发场景：100 万切换把 `STOCK_CAPITAL` 从 1,000,000 改为 400,000。

## 1. 缺陷（机器值）

当前 511360 持仓：**6,100 份 / 695,973 元**。

| STOCK_CAPITAL | tier | 目标 511360 | 当前持有 | 超额 |
|---:|---:|---:|---:|---:|
| 1,000,000 | 0.3 | 700,000 | 695,973 | −4,027（<2 万，不动） |
| 400,000 | 0.3 | 280,000 | 695,973 | **+415,973** |

`cash_sweep.py` 卖出分支在 `delta < 0` 时执行：

```python
oid = c.place_order(BOND_ETF, "sell", bond_shares, ref * 0.998)
```

即 **卖出全部 6,100 份（约 69.6 万）**，不是只卖超额 3,600 份。问题：

1. 切换后第一次 `run_daily.bat` 就会触发全卖；
2. 单笔约 69.6 万，远超“单笔订单 ≤10 万”红线；
3. 该路径未经过 `execution/risk.py` 的 `risk_check()`；
4. 卖光后次日又会按 28 万目标买回，产生无谓往返和滑点。

## 2. 最小修复（建议）

在 `cash_sweep.py` 增加纯函数（便于单测）：

```python
from config.settings import MAX_SINGLE_ORDER_VALUE

def _sell_qty(bond_shares, bond_held, target, ref, tier,
              max_value=MAX_SINGLE_ORDER_VALUE):
    """tier>0.5 才清空；降档/资本下调只卖超额，且单笔≤10万。"""
    if bond_shares <= 0 or ref <= 0:
        return 0
    if tier > 0.5:
        return int(bond_shares)              # 高仓位：维持原“清空”语义
    excess = float(bond_held) - float(target)
    if excess <= 0:
        return 0
    qty = int((excess / float(ref)) / 100) * 100
    cap = int((max_value / float(ref)) / 100) * 100
    return max(0, min(int(bond_shares), qty, cap))
```

卖出分支改为：

```python
    else:
        ref = (bond_held / bond_shares if bond_shares else _live_ref())
        qty = _sell_qty(bond_shares, bond_held, target, ref, tier)
        if qty <= 0:
            logger.info(f"{msg} 可卖数量 0，不动")
            return
        if dry_run:
            logger.info(f"{msg} [DRY] 拟卖{qty}份")
            return
        oid = c.place_order(BOND_ETF, "sell", qty, ref * 0.998)
        logger.info(f"{msg} 卖出{qty}份 → {oid}")
        send_alert(f"{msg}\n卖出 {BOND_ETF} {qty}份")
```

**预期行为（新口径）**：

| 场景 | 结果 |
|---|---|
| `STOCK_CAPITAL=400k`, tier=0.3, 6,100 份 @114 | 卖 **800 份**（≈9.1 万，≤10 万），分 5 天卖完 3,600 份 |
| tier>0.5 | 仍卖全部 6,100 份（保持“高仓位清空”语义；是否也分批另行裁定） |
| 差额 <2 万 | 不动（现有 MIN_AMOUNT 逻辑不变） |

## 3. 必须加的测试

1. `tier=0.3, held=696k, target=280k, ref=114, shares=6100` → `qty=800`（不是 6100）；
2. `tier=0.6, target=0` → `qty=6100`；
3. `excess<=0` → 0；
4. 单笔金额 `qty*ref <= 100_000`；
5. `ref<=0` / `shares=0` → 0。

## 4. 上线闸门

1. 先在 `STOCK_CAPITAL=400_000` 下跑 `python scripts/cash_sweep.py --dry-run`，
   必须显示 **拟卖 800 份**，不是 6,100 份；
2. 合并 `capital-live100w` 前，股票腿实际持仓≈0（当前 bear/0）；
3. 切换后首日只观察，不追单；卖出成交不足次日续卖（函数天然分批）；
4. 若用户不接受“单笔≤10 万”，需显式裁定 cash_sweep 的 ETF 豁免；
   在裁定前按红线执行。

## 5. 可直接应用的补丁

已生成统一 diff：`docs/cash_sweep_partial_sell.patch`（`git apply --check` 通过）。
在允许写 `scripts/cash_sweep.py` 的环境执行：

```bash
git apply docs/cash_sweep_partial_sell.patch
python3 -m pytest tests/test_cash_sweep_partial.py -q   # 若已补测试
```

补丁只改三处：导入 `MAX_SINGLE_ORDER_VALUE`、新增 `_sell_qty`、卖出分支按
`qty` 下单。应用后先在 `STOCK_CAPITAL=400_000` 下跑 `--dry-run`，
确认“拟卖 800 份”而不是 6,100 份。

## 6. 现状

- **2026-10-27 15:0x 已在 Windows `H:\quant` 应用补丁**（用户明确授权绕过
  deny）：Windows HEAD `e917ed3`，`git status` 显示
  `M scripts/cash_sweep.py`；`_sell_qty(6100,695973,280000,114,0.3)` 实测
  **800**；`--approve-tier3` 在 Windows 亦已就位（simulation=True /
  production=False）。
- **Linux 源码尚未提交该补丁**（授权文字限定 Windows）；21:00 Git-Sync
  的 53b631f..e917ed3 不含 cash_sweep，不会覆盖 Windows 本地修改。
  但为避免双源漂移，后续应在 Linux 提交同一补丁，或确认 Windows 为唯一
  属主；在提交前不要让其他 commit 触碰 `scripts/cash_sweep.py`。
- 100 万切换仍**延后到 11-02 收盘后**，且以 400k dry-run 通过为前置。
- 相关：`docs/capital_switch_100w_checklist.md`、
  `docs/cb_monthly_rebalance_runbook.md`、`logs/preflight_1029_1102_20261025.md`。
