"""
live-vs-backtest 对账（FWD-002，2026-09-14 实装）——"实盘是否在按主策略
运行"的逐日验证。规格 logs/live_backtest_reconcile_spec.md，用户裁定
优先级排在所有回测之前：快照断三天无人发现=该链路此前不存在，这类
错误比任何统计问题致命。

v1 范围（只读，可日更链外独立跑）:
  ① 名单 diff: 实盘持仓 vs 最近一次信号的目标持仓名单
  ② 权重 diff: 逐票 |w_live − w_target|（等权目标=1/N）
  ③ 现金档 diff: 实盘总仓位 vs 五档目标仓位（纯函数 get_position_ratio）
阈值（超限记录事件）: 名单差>2 或 权重差>5pp 或 档差>1 档。
快照缺失/陈旧当日 → 标记"对账不可行"并计入快照健康。
用法: python scripts/live_backtest_check.py [--report]
输出: logs/live_backtest_check.json + 控制台摘要
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.storage import load_meta
from run_backtest_a2 import get_position_ratio

ROOT = Path(__file__).parent.parent
SNAP = ROOT / "logs" / "qmt_positions_latest.json"
OUT = ROOT / "logs" / "live_backtest_check.json"


def _codes_from_positions(pos):
    """兼容 positions 的 list/dict 两种结构。"""
    out = []
    if isinstance(pos, list):
        for p in pos:
            if isinstance(p, dict):
                c = p.get("code") or p.get("stock_code")
                if c:
                    out.append(str(c))
    elif isinstance(pos, dict):
        out = [str(k) for k in pos.keys()]
    return out


def _leg(code):
    """腿分类: 12xxxx=可转债腿, 511360=扫尾单, 其余=股票腿(档位作用域)。"""
    code = str(code)
    if code.startswith("12"):
        return "cb"
    if code == "511360":
        return "sweep"
    return "stock"


def _weight_from_positions(pos):
    """逐票权重(市值口径; 无市值字段则退化为等权——盘前快照常见)。"""
    codes = _codes_from_positions(pos)
    n = max(len(codes), 1)
    mvs = {}
    if isinstance(pos, dict):
        for c, v in pos.items():
            if isinstance(v, dict):
                try:
                    mvs[str(c)] = float(v.get("market_value", 0) or 0)
                except (TypeError, ValueError):
                    pass
    elif isinstance(pos, list):
        for p in pos:
            if isinstance(p, dict):
                c = str(p.get("code") or p.get("stock_code") or "")
                try:
                    mvs[c] = float(p.get("market_value", 0) or 0)
                except (TypeError, ValueError):
                    pass
    if mvs and sum(mvs.values()) > 0:
        tot = sum(mvs.values())
        return {c: mvs.get(c, 0.0) / tot for c in codes}
    return {c: 1.0 / n for c in codes}


def _target_holdings():
    """目标=执行器实际消费的分组信号(g0+g1)的 holdings 并集。

    v2 (2026-09-17): 旧 glob 按文件名排序会拿到 signal_a_latest.json
    (09-01 陈旧 24 票) → bear 日对账恒报"名单差 0+24"假事件。改为
    只读执行链真输入 signal_a_g0/g1.json; 返回 (target, regime,
    signal_date, position_ratio)。"""
    target, regime, sig_date, ratio = [], None, None, None
    for g in ("g0", "g1"):
        p = ROOT / "data_store" / "meta" / f"signal_a_{g}.json"
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d.get("holdings"), list):
            target += [str(x) for x in d["holdings"]]
        regime = d.get("regime", regime)
        sig_date = d.get("signal_date", sig_date)
        ratio = d.get("position_ratio", ratio)
    if regime == "bear":
        target = []  # bear 信号持仓为空, 以信号为准
    return target, regime, sig_date, ratio


def _tier_position_today():
    """五档目标仓位(纯函数, 与实盘信号链同实现)。"""
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    if ic.empty:
        return None
    return get_position_ratio(ic, ic.index[-1])


def main(report=False):
    res = {"ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}
    if not SNAP.exists():
        res["status"] = "快照缺失, 对账不可行"
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(json.dumps(res, ensure_ascii=False))
        return

    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    exported = str(snap.get("exported_at", ""))[:10]
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    if exported != today:
        res["status"] = f"快照陈旧({exported}), 对账不可行"
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(json.dumps(res, ensure_ascii=False))
        return

    pos = snap.get("positions", [])
    live_codes = _codes_from_positions(pos)
    live_w = _weight_from_positions(pos)
    target, sig_regime, sig_date, tier_pos = _target_holdings()

    # 档位作用域=股票腿(CB腿/扫尾单不计入; 信号 holdings 亦为股票腿名单)
    stock_codes = [c for c in live_codes if _leg(c) == "stock"]
    stock_w = sum(live_w.get(c, 0.0) for c in stock_codes)
    cb_codes = [c for c in live_codes if _leg(c) == "cb"]

    list_extra = sorted(set(stock_codes) - set(target))
    list_missing = sorted(set(target) - set(stock_codes))
    w_diffs = {c: round(abs(live_w.get(c, 0.0) - 1.0 / max(len(target), 1)), 4)
               for c in set(stock_codes) | set(target)
               if abs(live_w.get(c, 0.0) - 1.0 / max(len(target), 1)) > 0.05}

    events = []
    if sig_date != today:
        events.append(f"信号陈旧: signal_date={sig_date} ≠ {today}")
    if len(list_extra) + len(list_missing) > 2:
        events.append(f"名单差 {len(list_extra)}+{len(list_missing)} > 2")
    if w_diffs:
        events.append(f"权重差超5pp: {len(w_diffs)}票")
    # bear=股票腿清仓, 目标=0 而非 position_ratio(0.3 是总资金档位,
    # 股票腿执行语义=清仓, 见 trader._execute_bear)
    target_stock_pos = 0.0 if sig_regime == "bear" else (tier_pos or 0.0)
    if abs(stock_w - target_stock_pos) > 0.20:
        events.append(f"现金档差: 股票腿{stock_w:.0%} vs 目标{target_stock_pos:.0%}")

    res.update({
        "status": "ok" if not events else "事件",
        "snapshot": exported,
        "live_codes": sorted(live_codes),
        "cb_codes": sorted(cb_codes),
        "target_codes": sorted(target),
        "list_extra": list_extra, "list_missing": list_missing,
        "weight_diff_gt5pp": w_diffs,
        "stock_leg_pos": round(stock_w, 3), "tier_pos": tier_pos,
        "sig_regime": sig_regime, "signal_date": sig_date,
        "events": events,
        "note": "盘前快照市值可能为零(等权退化); 权威对账以 15:12 收盘快照为准",
    })
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if report and events:
        from monitoring.alerts import send_alert
        send_alert("live对账事件: " + "; ".join(events), level="warning")


if __name__ == "__main__":
    main(report="--report" in sys.argv)
