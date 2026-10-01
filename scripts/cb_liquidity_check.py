"""CB双低 持仓券流动性分布 (2026-09-22 用户追问: 405%/年换手, 3.8万单券买入
是否会推动价格——回测按收盘价成交, 冲击成本未建模)
口径: V3 N=10 甲方案选股, 对每次月度调仓的每只入选券, 取调仓日成交额
(本地日线无amount列, amount=volume×close, 成交量单位为张——新浪实时实测
成交额/成交量=93.3≈现价验证)。输出: 全实例成交额分布 + 3.8万买入占比。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta
from data.cb_select import select_double_low

REAL_COST = 0.001
TOP_N = 25
N_DAYS = 10

logger.info("加载快照...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

price_panel = {}
for code in snap_codes:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists():
        continue
    df = pd.read_parquet(fpath)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    prev = None
    for _, row in df.iterrows():
        d = row["date"]
        p = float(row["close"]) if "close" in row else None
        if p and p > 0:
            if prev is not None and abs(p / prev - 1) > 0.5:
                continue
            price_panel.setdefault(d, {})[code] = p
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板: {len(price_panel)}交易日")


# 选股过滤共用 data/cb_select (闸门二十一)


# ── 逐月选股(甲: 补足25), 收集 (调仓日, code) ──
# 2026-09-28 扩容评估(ed1a430/836510b 预登记): 同时收集卖出实例——
# 调仓日轮出券(前一期持仓∩新名单补集), 硬约束第四条(卖出侧占比须更严)。
instances = []      # 买入实例 (date_str, code)
sell_instances = []  # 卖出实例 (date_str, code)
snap_idx = 0
last_snap_date = None
prev_selected = None
for i, date in enumerate(all_dates):
    date_str = str(date.date())[:10]
    prices_today = price_panel.get(date, {})
    while (snap_idx < len(snap_dates)
           and str(snap_dates[snap_idx].date())[:10] <= date_str):
        last_snap_date = snap_dates[snap_idx]
        snap_idx += 1
    if last_snap_date and str(last_snap_date.date())[:10] == date_str:
        snap = snapshots[snapshots["snap_date"] == last_snap_date]
        if not snap.empty:
            avails = [c for c in snap["code"].unique() if c in prices_today]
            selected = select_double_low(
                snap[snap["code"].isin(avails)], top=TOP_N)
            if len(selected) >= TOP_N:
                for c in selected:
                    instances.append((date_str, c))
                if prev_selected is not None:
                    for c in prev_selected:
                        if c not in selected:
                            sell_instances.append((date_str, c))
                prev_selected = selected

logger.info(f"  买入实例: {len(instances)} (券,月), 卖出实例: {len(sell_instances)}")

# ── 读取入选/轮出券的 (date, volume), 计算 amount=volume×close ──
union = sorted({c for _, c in instances + sell_instances})
vol_map = {}
no_vol_cols = set()
for code in union:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists():
        continue
    df = pd.read_parquet(fpath)
    if "volume" not in df.columns:
        no_vol_cols.add(code)
        continue
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    vol_map[code] = dict(zip(df["date"], df["volume"]))
if no_vol_cols:
    logger.warning(f"  {len(no_vol_cols)}只券无volume列: {sorted(no_vol_cols)[:10]}")


def collect_amounts(inst):
    """返回 (金额数组, 缺失数, 对齐后的实例列表)——金额数组跳过缺失行,
    必须保留对齐实例供最差例回标(索引错位=标签错贴, 2026-09-28 实测)。"""
    out, miss, kept = [], 0, []
    for date_str, code in inst:
        v = vol_map.get(code, {}).get(date_str)
        cp = price_panel.get(pd.Timestamp(date_str), {}).get(code)
        if v is None or not cp:
            miss += 1
            continue
        out.append(float(v) * float(cp))
        kept.append((date_str, code))
    return np.array(out), miss, kept


amounts, missing, kept_inst = collect_amounts(instances)
sell_amounts, sell_missing, kept_sell = collect_amounts(sell_instances)
# 对齐守卫(2026-09-28 标签错位教训): 金额数组与标签数组长度必须一致,
# 任一缺失行跳过都会造成"最差例贴错券"——数值不变归属变, 最难发现的错位。
assert len(amounts) == len(kept_inst), "买入金额/标签对齐失败"
assert len(sell_amounts) == len(kept_sell), "卖出金额/标签对齐失败"

print("=" * 80)
print(f"CB双低调仓日成交额分布 (买入实例 N={len(amounts)}缺{missing}, "
      f"卖出实例 N={len(sell_amounts)}缺{sell_missing})")
print("=" * 80)
for p in [1, 5, 10, 25, 50, 75, 90]:
    print(f"  P{p:>2}: 买入 {np.percentile(amounts, p)/1e4:>11.0f} 万元"
          f" | 卖出 {np.percentile(sell_amounts, p)/1e4:>11.0f} 万元")

# ── 扩容三档占比 (2026-09-28, ed1a430 预登记: 60/80/100万 → 2.4/3.2/4.0万/券;
#    3.8万=96万满配旧口径仅作参考线) ──
TIERS = [("60万档 2.4万/券", 24_000),
         ("80万档 3.2万/券", 32_000),
         ("100万档 4.0万/券", 40_000),
         ("参考:96万满配 3.8万/券(旧审计口径)", 38_000)]


def tier_report(label, buy_amt):
    r_buy = buy_amt / amounts
    r_sell = buy_amt / sell_amounts
    print(f"\n【{label}】 买入占比(硬约束: P95<3%) / 卖出占比(硬约束: P95<3% 且最差单例<15%):")
    for p in [50, 90, 95, 99]:
        print(f"  P{p:>2}: 买入 {np.percentile(r_buy, p)*100:6.2f}%"
              f" | 卖出 {np.percentile(r_sell, p)*100:6.2f}%")
    print(f"  买入占比>3%实例: {(r_buy > 0.03).mean()*100:5.1f}%"
          f" ({int((r_buy > 0.03).sum())}/{len(r_buy)})")
    print(f"  卖出占比>3%实例: {(r_sell > 0.03).mean()*100:5.1f}%"
          f" ({int((r_sell > 0.03).sum())}/{len(r_sell)})")
    ws = np.argsort(r_sell)[-5:][::-1]
    print("  卖出最差5例(调仓日, 券, 成交额, 占比):")
    for idx in ws:
        print(f"    {kept_sell[idx][0]} {kept_sell[idx][1]} "
              f"{sell_amounts[idx]/1e4:>9.0f}万 {r_sell[idx]*100:5.2f}%")
    # 分年份 P99 序列(约束修订草案第四节"分年份条款"测量件, 2026-10-01):
    # 独立评估"不同市场状态下约束是否该不同"; 任一年 P99>20% 触发单独说明
    yrs = [k[0][:4] for k in kept_sell]
    print(f"  卖出占比分年份 P99(条款: 任一年>20% 单独说明):")
    for y in sorted(set(yrs)):
        ry = r_sell[np.array([k[0][:4] for k in kept_sell]) == y]
        p99y = np.percentile(ry, 99) if len(ry) >= 5 else float("nan")
        flag = " ⚠>20%单独说明" if (not np.isnan(p99y) and p99y > 0.20) else ""
        print(f"    {y}: P99={p99y*100:5.2f}% (N={len(ry)}){flag}")


for label, amt in TIERS:
    tier_report(label, amt)
