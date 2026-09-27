"""八步法生死测试第1步: 隔夜跳空分布 (2026-09-27 预登记后首跑, 判据已锁)。

构造(预登记 2026-09-27, 见账本"八步法复核预登记"):
  全市场, 当日 pct_chg∈[3%,5%] 且 close/high≥0.98(尾盘创新高近似),
  排除停牌/黑名单/OHLC 自洽异常; 买入价=T 收盘。
两问(判据数值已锁, 开跑前落盘):
  P① = P(T+1 high ≥ 买入价×1.03)          —— 止盈限价单成交概率(摸价口径上界)
  P② = P(T+1 open ≤ 买入价×0.985)          —— 低开止损触发概率
  继续条件: P①≥0.45 且 P①−P②≥0.15; 任一不满足 → 八步法死亡, 后七步为装饰。
附加报告: P(低开)/P(高开)、gap 分布、最高比分布、逐年稳定性。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
logger.remove()

from data.storage import load_daily
from backtest_return_attribution import load_blacklist

END_20260924 = "2026-09-24"   # 工件 as_of 冻结(闸门十三)
START = "2019-01-01"
TP_RATIO = 1.03
SL_RATIO = 0.985
CLOSE_HIGH_MIN = 0.98

blacklist = load_blacklist()
logger.info(f"黑名单 {len(blacklist)} 只")

meta = pd.read_parquet("data_store/meta/stock_info_full.parquet") \
    if Path("data_store/meta/stock_info_full.parquet").exists() else None
codes = meta["code"].tolist() if meta is not None and not meta.empty else []
if not codes:
    # 兜底: 全市场文件列表
    from data.storage import load_meta
    m = load_meta("stock_info_full")
    codes = m["code"].tolist()

logger.info(f"全市场 {len(codes)} 只, 加载 OHLC...")
rows = []
n_loaded = 0
for code in codes:
    if code in blacklist:
        continue
    try:
        df = load_daily(code, START, END_20260924)
    except Exception:
        continue
    if df.empty:
        continue
    df = df.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "pct_chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "pct_chg"])
    if len(df) < 250:
        continue
    n_loaded += 1
    # 自洽守卫: close∈[low,high]、high≥open、low≤open(容 1bp 尾差)
    ok = ((df["close"] >= df["low"] * 0.999) & (df["close"] <= df["high"] * 1.001)
          & (df["high"] >= df["open"] * 0.999) & (df["low"] <= df["open"] * 1.001))
    df = df[ok]
    # 停牌过滤
    if "is_suspended" in df.columns:
        df = df[df["is_suspended"].astype(str).str.lower() != "true"]
    if df.empty:
        continue
    # 当日条件: pct_chg∈[3,5] 且 close/high≥0.98 且非极端(振幅守卫)
    cond = (df["pct_chg"] >= 3.0) & (df["pct_chg"] <= 5.0) \
        & (df["close"] / df["high"] >= CLOSE_HIGH_MIN) \
        & (df["high"] / df["close"] < 1.11) & (df["close"] / df["low"] < 1.11)
    idx = np.where(cond.values)[0]
    if len(idx) == 0:
        continue
    n = len(df)
    o = df["open"].values
    h = df["high"].values
    c = df["close"].values
    for i in idx:
        if i + 1 >= n:
            continue  # 样本末端无次日(闸门二十二: 数据边界不是事件)
        buy = c[i]
        rows.append({
            "date": str(df["date"].values[i])[:10],
            "code": code,
            "buy": buy,
            "n_open": o[i + 1],
            "n_high": h[i + 1],
            "tp_hit": h[i + 1] >= buy * TP_RATIO,
            "sl_hit": o[i + 1] <= buy * SL_RATIO,
            "gap_down": o[i + 1] < buy,
            "gap_up": o[i + 1] > buy,
        })
logger.info(f"加载 {n_loaded} 只, 命中样本 {len(rows)} 个")

if not rows:
    print("无样本")
    sys.exit(0)

d = pd.DataFrame(rows)
p1 = d["tp_hit"].mean()
p2 = d["sl_hit"].mean()
se1 = np.sqrt(p1 * (1 - p1) / len(d))
se2 = np.sqrt(p2 * (1 - p2) / len(d))
p_down = d["gap_down"].mean()
p_up = d["gap_up"].mean()
gap = d["n_open"] / d["buy"] - 1
hr = d["n_high"] / d["buy"] - 1

print("=" * 84)
print(f"八步法第1步 隔夜跳空分布 | as_of={END_20260924} | n={len(d)}")
print("=" * 84)
print(f"P① 止盈触发 P(T+1 high ≥ 买入×1.03) = {p1*100:.1f}% ± {se1*100:.1f}%")
print(f"P② 止损触发 P(T+1 open ≤ 买入×0.985) = {p2*100:.1f}% ± {se2*100:.1f}%")
print(f"P① − P② = {(p1-p2)*100:+.1f}pp")
print(f"低开概率 {p_down*100:.1f}% | 高开概率 {p_up*100:.1f}% (平开 {100-(p_down+p_up)*100:.1f}%)")
print(f"次日开盘 gap: 中位 {np.median(gap)*100:+.2f}% 均值 {gap.mean()*100:+.2f}%")
print(f"次日最高比:   中位 {np.median(hr)*100:+.2f}% P90 {np.percentile(hr,90)*100:+.2f}%")
print()
print("[判据执行(已锁, 开跑前落盘)]")
ok = p1 >= 0.45 and (p1 - p2) >= 0.15
print(f"  P①≥0.45: {'✓' if p1>=0.45 else '✗'} ({p1*100:.1f}%) | "
      f"P①−P②≥0.15: {'✓' if p1-p2>=0.15 else '✗'} ({(p1-p2)*100:+.1f}pp)")
print(f"★ 判定: {'继续(可跑第2步三因子IC)' if ok else '八步法死亡——后七步为装饰, 整件事终止'}")
print()
print("[逐年稳定性]")
print(f"{'年份':<8}{'样本':>8}{'P①':>8}{'P②':>8}{'差':>8}")
for y, g in d.groupby(d["date"].str[:4]):
    a, b = g["tp_hit"].mean(), g["sl_hit"].mean()
    print(f"{y:<8}{len(g):>8}{a*100:>7.1f}%{b*100:>7.1f}%{(a-b)*100:>+7.1f}pp")

out = d[["date", "code", "buy", "n_open", "n_high", "tp_hit", "sl_hit"]]
out.to_csv("logs/overnight_gap_samples.csv", index=False)
print(f"\n样本明细已存 logs/overnight_gap_samples.csv ({len(out)} 行)")
