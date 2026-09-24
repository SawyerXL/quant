"""CB 成交滑点信息行 (2026-09-24, vs 信号参考价口径——仅信息展示,
非 measure_execution_costs 的裁决口径[当日收盘价, §6.8 钉死])。
裁决口径的 CB 参考价路径已加(当日收盘, 见 measure_execution_costs.load_close),
CB 日线日更恢复后自动可测。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

for day in ["20260921", "20260924"]:
    of = Path(f"logs/qmt_orders_{day}.json")
    if not of.exists():
        print(day, "无订单文件")
        continue
    d = json.loads(of.read_text(encoding="utf-8"))
    sig = json.loads(Path("data_store/meta/signal_cb_latest.json").read_text(encoding="utf-8"))
    sig_day = str(sig.get("signal_date", ""))
    stale = "⚠️非同日信号, 混入信号漂移, 仅供方向参考" if sig_day != day else "同日信号, 严格可比"
    print(f"\n{day} 订单 {d.get('order_count')} 笔 | 信号日期 {sig_day} | {stale}")
    sp = sig.get("prices", {})
    tot = wslip = 0.0
    rows = []
    for o in d.get("orders", []):
        code = str(o.get("code", "")).split(".")[0]
        side = o.get("direction", "")
        filled = int(o.get("filled", 0) or 0)
        px = float(o.get("fill_price", 0) or 0)
        # 只统计已成交单: 未成交单(filled=0)的 limit_price 是委托价不是成交价,
        # 混入会把废单/挂单的限价差算成"滑点"(09-21 首版教训)
        if filled <= 0 or px <= 0:
            continue
        ref = float(sp.get(code, 0) or 0)
        if ref <= 0:
            rows.append((code, side, px, ref, None, filled))
            continue
        bp = (px / ref - 1) * 1e4 if side == "buy" else (1 - px / ref) * 1e4
        notional = px * filled
        tot += notional
        wslip += bp * notional
        rows.append((code, side, px, ref, bp, filled))
    print(f"{'code':<8}{'方向':<5}{'成交价':>9}{'信号价':>9}{'滑点bp':>8}{'张数':>6}")
    for code, side, px, ref, bp, sh in rows:
        bps = f"{bp:+8.1f}" if bp is not None else "     —"
        print(f"{code:<8}{side:<5}{px:>9.3f}{ref:>9.3f}{bps:>8}{sh:>6}")
    if tot > 0:
        print(f"成交额加权滑点(vs 信号价): {wslip / tot:+.2f} bp | 成交额 {tot:,.0f} 元")
