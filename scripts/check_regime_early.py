"""
市场趋势早期预警：比MA200更早感知牛熊转换。

三个指标：
  ① 连跌周数 ≥ 3（持续走弱）
  ② 波动率短期/长期 > 1.5x（恐慌放大）
  ③ CSI800 跌破 MA60（中期趋势打破）

信号等级：
  🟢 正常：0 个触发
  🟡 关注：1 个触发
  🟠 警戒：2 个触发 → 建议降仓至70%
  🔴 危险：3 个触发 → 建议降仓至50%并暂停新买入

用法：python scripts/check_regime_early.py
Cron: 0 18 * * 1-5 (收盘后运行)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date
from loguru import logger

logger.add("logs/regime_early.log", rotation="1 week", retention="90 days")


def check() -> dict:
    """运行三项检查，返回结果字典。"""
    import akshare as ak

    # 数据
    df = ak.stock_zh_index_daily(symbol="sh000906")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    close = df["close"]

    today      = close.index[-1]
    today_str  = today.strftime("%Y-%m-%d")

    results = {"date": today_str, "close": float(close.iloc[-1]), "warnings": []}

    # ── ① 连跌周数 ───────────────────────────────────────
    weekly     = close.resample("W-FRI").last().dropna()
    weekly_ret = weekly.pct_change().dropna()
    consec_down = 0
    for r in reversed(weekly_ret.values):
        if r < 0: consec_down += 1
        else: break
    recent_4w  = [f"{v:+.2%}" for v in weekly_ret.tail(4).values]
    results["consec_down_weeks"] = consec_down
    results["recent_weekly_ret"] = recent_4w
    if consec_down >= 3:
        results["warnings"].append(f"连跌{consec_down}周 ({', '.join(recent_4w)})")

    # ── ② 波动率放大 ──────────────────────────────────────
    rets    = close.pct_change().dropna()
    vol_5d  = float(rets.tail(5).std()  * np.sqrt(252))
    vol_20d = float(rets.tail(20).std() * np.sqrt(252))
    vol_60d = float(rets.tail(60).std() * np.sqrt(252))
    vol_ratio = vol_5d / vol_60d if vol_60d > 0 else 1.0
    results["vol_5d"]   = round(vol_5d, 3)
    results["vol_20d"]  = round(vol_20d, 3)
    results["vol_ratio"] = round(vol_ratio, 2)
    if vol_ratio > 1.5:
        results["warnings"].append(f"波动率急剧放大({vol_ratio:.1f}x)")

    # ── ③ MA60 ────────────────────────────────────────────
    ma60  = close.rolling(60).mean()
    ma200 = close.rolling(200).mean()
    ratio = float(close.iloc[-1] / ma200.iloc[-1])
    below_ma60  = float(close.iloc[-1]) < float(ma60.iloc[-1])
    below_ma200 = float(close.iloc[-1]) < float(ma200.iloc[-1])
    results["ma60"]      = round(float(ma60.iloc[-1]), 0)
    results["ma200"]     = round(float(ma200.iloc[-1]), 0)
    results["ratio_ma200"] = round(ratio, 3)
    results["below_ma60"]  = below_ma60
    if below_ma60:
        results["warnings"].append(f"跌破MA60({results['ma60']:.0f})")
    if below_ma200:
        results["warnings"].append(f"跌破MA200({results['ma200']:.0f})")

    # ── 信号等级 ───────────────────────────────────────────
    n = len(results["warnings"])
    if n == 0:
        level, emoji, advice = "正常", "🟢", "正常持仓，无需调整"
    elif n == 1:
        level, emoji, advice = "关注", "🟡", "关注走势，暂不调整仓位"
    elif n == 2:
        level, emoji, advice = "警戒", "🟠", "建议降仓至70%，减少新买入"
    else:
        level, emoji, advice = "危险", "🔴", "建议降仓至50%并暂停新买入"

    results["level"]  = level
    results["emoji"]  = emoji
    results["advice"] = advice
    return results


def main():
    r = check()

    print(f"\n{'='*60}")
    print(f"  市场趋势早期预警  {r['date']}")
    print(f"{'='*60}")
    print(f"  CSI800: {r['close']:.0f}  MA60: {r['ma60']:.0f}  "
          f"MA200: {r['ma200']:.0f}  Ratio: {r['ratio_ma200']:.3f}")
    print(f"  连跌周数: {r['consec_down_weeks']}周  "
          f"波动率: {r['vol_5d']:.0%}(5d)/{r['vol_20d']:.0%}(20d)  "
          f"波动比: {r['vol_ratio']:.1f}x")
    print(f"\n  {r['emoji']} 信号等级: {r['level']}")
    print(f"  建议: {r['advice']}")
    if r['warnings']:
        print(f"  触发指标:")
        for w in r['warnings']:
            print(f"    ⚠️  {w}")

    # ── 推送告警 ───────────────────────────────────────────
    if r['level'] in ('警戒', '危险'):
        try:
            from monitoring.alerts import send_alert
            send_alert(
                f"【{r['emoji']} 市场预警 {r['level']}】{r['date']}\n"
                f"CSI800: {r['close']:.0f}  Ratio: {r['ratio_ma200']:.3f}\n"
                + "\n".join(f"⚠️ {w}" for w in r['warnings']) +
                f"\n\n建议: {r['advice']}"
            )
        except Exception as e:
            logger.warning(f"告警推送失败: {e}")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
