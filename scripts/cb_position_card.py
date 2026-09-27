"""CB 持仓明细卡 + 09-30 换血后容量算术 (2026-09-27, 甲/乙决策材料用)。
读最新持仓快照, 输出 12 只持仓市值 + 换血后估计(卖5买19 预览口径)。
"""
import json
from pathlib import Path

snap = json.loads(Path("logs/qmt_positions_latest.json").read_text(encoding="utf-8"))
pos = snap.get("positions", {})
rows = []
for c, v in sorted(pos.items()):
    if isinstance(v, dict) and v.get("volume", 0) > 0 and str(c)[:3] in ("110", "111", "113", "118", "123", "127", "128"):
        mv = v.get("market_value", 0) or (v.get("volume", 0) * v.get("price", 0))
        rows.append((c.split(".")[0], int(v.get("volume", 0)), mv))

tot = sum(r[2] for r in rows)
print(f"快照 exported_at: {snap.get('exported_at')}")
print(f"{'code':<8}{'张数':>6}{'市值':>12}")
for c, q, mv in rows:
    print(f"{c:<8}{q:>6}{mv:>12,.0f}")
print(f"{'合计':<8}{'':>6}{tot:>12,.0f}")

# 09-30 换血后估计(09-24 预览口径: 卖 5 只/买 19 笔 43.7 万)
sells = {"110081": 220, "113056": 180, "113062": 180, "113681": 170, "127040": 188}
sell_mv = sum(mv for c, q, mv in rows if c in sells)
buy_mv = 437_000  # 09-24 预览 19 笔买入合计(516,128 总交易额 − 卖)
post = tot - sell_mv + buy_mv
print(f"\n换血算术(09-24 预览口径): 卖出 {sell_mv:,.0f} + 买入 ≈{buy_mv:,.0f} → "
      f"换血后 CB 腿 ≈ {post:,.0f} vs 60 万计划 → 缺口 {(600_000-post)/10_000:.1f} 万")
