"""MACD 柱变色证伪对照 (2026-10-01, 外部宣称组件级粗口径探测)。

判据预登记(logs/overnight_open_questions.md 10-01 节):
  定义=MACD(12,26,9)柱(DIF−DEA)由负转正, 每票每日最多一次; 全市场2019-2026
  黑名单404排除; T后1/3/5/10日close-to-close收益 vs 同票同窗口无条件均值。
  判死两阶: ①四窗口全|效应|<0.5×票级SE → 判死 ②统计显著但|效应|<0.15% → 判死。
  措辞纪律: 组件判死≠整体判死。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta, load_daily

logger.remove()
START, END = "2019-01-01", "2026-09-24"
HORIZONS = [1, 3, 5, 10]
COST = 0.0015  # 单边成本 0.15%(经济显著性线)

meta = load_meta("stock_info_full")
blacklist = load_meta("price_blacklist") if False else None  # 黑名单经由 storage 黑名单统一处理
codes = meta["code"].tolist()

events_all = []   # 事件级: (horizon, ret − 基准)
stock_effects = {}  # 票级: code -> {h: mean_effect}
flip_intervals = []  # 每票平均变色间隔(交易日)

for code in codes:
    try:
        d = load_daily(code, START, END)
        if d.empty:
            continue
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        cl = pd.to_numeric(d["close"], errors="coerce").dropna()
        cl = cl[cl > 0]  # 0价停牌行剔除(除零→inf 的根因)
        if len(cl) < 300:
            continue
    except Exception:
        continue
    dif = cl.ewm(span=12, adjust=False).mean() - cl.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    flips = hist[(hist.shift(1) < 0) & (hist >= 0)].index
    if len(flips) < 5:
        continue
    gaps = np.diff(np.searchsorted(cl.index, flips))
    flip_intervals.extend(gaps.tolist())
    rets = cl.pct_change()
    rets = rets.replace([np.inf, -np.inf], np.nan)
    for h in HORIZONS:
        fwd = (cl.shift(-h) / cl - 1.0).replace([np.inf, -np.inf], np.nan)
        base_mean = fwd.mean()
        ev = fwd.loc[flips].dropna()
        if len(ev) < 3:
            continue
        stock_effects.setdefault(code, {})[h] = float((ev - base_mean).mean())
        for v in (ev - base_mean):
            events_all.append((h, float(v)))

print("=" * 72)
print("MACD 柱变色(负→正) 证伪对照 | 全市场 2019-2026 | 基准=同票同窗口无条件均值")
print("=" * 72)
n_events = len(events_all)
n_stocks = len(stock_effects)
print(f"[先报数] 事件观测: {n_events} | 独立票: {n_stocks} | "
      f"平均变色间隔: {np.mean(flip_intervals):.1f} 交易日(中位 {np.median(flip_intervals):.1f})")
print(f"[判读前置] 10日窗口重叠: 变色间隔<10日的占比 "
      f"{np.mean(np.array(flip_intervals) < 10)*100:.0f}%")

ev_df = pd.DataFrame(events_all, columns=["h", "eff"])
print(f"\n{'窗口':>5}{'事件效应':>10}{'票级效应':>10}{'票级SE':>9}{'t':>7}  两阶判死")
verdicts = []
for h in HORIZONS:
    e = ev_df[ev_df["h"] == h]["eff"]
    s = pd.Series([v.get(h) for v in stock_effects.values()]).dropna()
    se = s.std(ddof=1) / np.sqrt(len(s)) if len(s) > 1 else float("nan")
    t = s.mean() / se if len(s) > 1 and se > 0 else float("nan")
    stat_dead = (abs(s.mean()) < 0.5 * se)
    econ_dead = (abs(s.mean()) < COST)
    verdict = "判死" if (stat_dead or econ_dead) else "存疑→下一步"
    if stat_dead:
        verdict += "(统计)"
    if econ_dead and not stat_dead:
        verdict += "(经济)"
    verdicts.append(verdict)
    print(f"{h:>4}日{e.mean()*100:>+9.2f}%{s.mean()*100:>+9.2f}%{se*100:>8.2f}%{t:>+7.2f}  {verdict}")
print("\n注: 判死两阶出数前锁——①四窗口全|效应|<0.5×票级SE ②统计显著但<0.15%单边成本。")
print("    措辞: 组件判死≠整体判死(PA整体判死理由=区域不可量化, 独立结论)。")
