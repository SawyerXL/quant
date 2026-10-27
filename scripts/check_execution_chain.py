"""14:50 执行链只读检查（2026-10-27）。

为什么需要：14:30 `run_daily.bat` 依次跑 track a → track cb；14:45 的
`check_exec.ps1` 只补 track a，不补 cb。若 14:30 链在 cb 之前中断，CB
没有自动重试；11-02 首次月度会静默漏调。本检查只读信号/执行结果，发现
“有信号、无结果”就告警，不修改订单、不重试。

判定：
- 股票 a：g0/g1 合并后的 buy/sell 非空 → 必须有当天 execution_result_a；
- CB：rebalance=true 或 buy/sell 非空 → 必须有当天 execution_result_cb；
- 无 buy/sell 且非 rebalance 的日子，不要求结果（避免非调仓日误报）；
- signal_date 不是今天 → 单独告警（信号链没刷新，执行器会 skip）。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
META = ROOT / "data_store" / "meta"
RESULTS = ROOT / "logs"
STATE = ROOT / "logs" / "exec_check_state.json"


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_date(s):
    return str(s or "").replace("-", "")[:8]


def evaluate(today: str, signals: dict, results: dict):
    """纯函数：返回 (issues, warnings, notes)。today=YYYYMMDD。"""
    issues, warnings, notes = [], [], []
    for track in ("a", "cb"):
        sig = signals.get(track)
        if not sig:
            issues.append(f"{track}: 信号文件缺失/不可读")
            continue
        sig_date = _norm_date(sig.get("signal_date"))
        if sig_date != today:
            issues.append(f"{track}: signal_date={sig_date} ≠ 今天 {today}（信号链未刷新，执行器会 skip）")
        buys = sig.get("buy") or []
        sells = sig.get("sell") or []
        needs = bool(buys or sells) or (track == "cb" and bool(sig.get("rebalance")))
        if not needs:
            notes.append(f"{track}: 无 buy/sell，不要求 execution_result")
            continue
        res = results.get(track)
        if not res:
            issues.append(f"{track}: 有 buy/sell/rebalance 但缺少 execution_result")
            continue
        if _norm_date(res.get("signal_date")) != today:
            issues.append(f"{track}: execution_result.signal_date={res.get('signal_date')} ≠ 今天")
        fr = res.get("fill_rate_pct")
        if fr is not None and float(fr) < 100:
            warnings.append(f"{track}: fill_rate={fr}% (<100%)")
        blocked = res.get("blocked") or []
        skipped = res.get("skipped_rejected") or []
        if blocked:
            warnings.append(f"{track}: blocked={len(blocked)} 笔")
        if skipped:
            warnings.append(f"{track}: skipped_rejected={len(skipped)} 笔")
    return issues, warnings, notes


def _stock_signal():
    s0 = _load(META / "signal_a_g0.json")
    s1 = _load(META / "signal_a_g1.json")
    if not s0 or not s1:
        return None
    if _norm_date(s0.get("signal_date")) != _norm_date(s1.get("signal_date")):
        return {"signal_date": "MISMATCH", "buy": ["g0/g1 date mismatch"],
                "sell": [], "rebalance": False}
    return {
        "signal_date": s0.get("signal_date"),
        "buy": sorted(set(s0.get("buy") or []) | set(s1.get("buy") or [])),
        "sell": sorted(set(s0.get("sell") or []) | set(s1.get("sell") or [])),
        "rebalance": bool(s0.get("rebalance") or s1.get("rebalance")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    args = ap.parse_args()
    today = _norm_date(args.date)
    signals = {"a": _stock_signal(),
               "cb": _load(META / "signal_cb_latest.json")}
    results = {
        "a": _load(RESULTS / f"execution_result_{today}_a.json"),
        "cb": _load(RESULTS / f"execution_result_{today}_cb.json"),
    }
    issues, warnings, notes = evaluate(today, signals, results)
    state = {"checked_at": str(__import__("datetime").datetime.now()),
             "date": today, "issues": issues, "warnings": warnings,
             "notes": notes}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    for x in notes:
        logger.info(x)
    for x in warnings:
        logger.warning(x)
    for x in issues:
        logger.error(x)
    if issues or warnings:
        from monitoring.alerts import send_alert
        level = "error" if issues else "warning"
        send_alert("[执行链检查] " + "; ".join(issues + warnings), level=level)
    else:
        logger.info("执行链检查通过：有信号的 track 均有当天结果")
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
