"""
配置一致性检查（2026-09-08 建，第零道闸门第四项）——引擎回测配置 vs
实盘信号参数 vs spec 定案 的逐参数对齐。教训: TP 三方漂移(引擎 25/50、
spec v3 30/60、实盘 v4 30/45全清+兜底)——回测测的不是实盘在跑的东西,
且不报错、数字合理、能通过所有闸门。任何回测结论引用前须声明已对齐。
用法: python scripts/check_config_alignment.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

# 引擎默认(回测实际使用的 DEFAULT_CONFIG, 非类默认)
from backtest_config import DEFAULT_CONFIG
ENGINE = DEFAULT_CONFIG

# 实盘信号参数(daily_signal_a_v2 常量)
import daily_signal_a_v2 as sig
LIVE = {
    "pool_size": getattr(sig, "N_HOLDINGS", None),
    "max_vol20": getattr(sig, "MAX_VOL20", None),
    "ma10_exit_days": getattr(sig, "MA10_EXIT_DAYS", None),
}

# spec 定案口径(qmt_strategy_spec 手动登记, 改动须过参数三件套)
SPEC = {
    "pool_size": 30,
    "max_vol20": 5.0,
    "ma10_exit_days": 4,
    "take_profit_1": 0.30,   # v3 定案 +30%
    "take_profit_2": 0.60,   # v3 定案 +60%
    "ma200_thresh_shift": -0.03,
    "commission": 0.0013,
}


def main():
    rows = []
    for key in sorted(set(list(ENGINE.to_dict().keys()) + list(LIVE.keys()) + list(SPEC.keys()))):
        if key in ("enable_stops", "enable_ma10_exit", "enable_take_profit"):
            continue  # 开关类单独登记(TP已关)
        e = getattr(ENGINE, key, None)
        l = LIVE.get(key)
        s = SPEC.get(key)
        # 只报关键决策参数(其余引擎内部参数不比对)
        if s is None and l is None:
            continue
        aligned = (e == s == l) if (e is not None and s is not None and l is not None) else None
        rows.append((key, e, l, s, aligned))

    print(f"{'参数':<22}{'引擎':>10}{'实盘':>10}{'spec':>10}  状态")
    bad = 0
    for key, e, l, s, aligned in rows:
        mark = "✅" if aligned else ("—" if aligned is None else "❌ 漂移")
        if aligned is False:
            bad += 1
        print(f"{key:<22}{str(e):>10}{str(l):>10}{str(s):>10}  {mark}")
    print(f"\n漂移项: {bad}")
    # 开关类登记(2026-09-08: 实盘TP已关)
    print("开关: 实盘 TP_ENABLED=False (2026-09-08 关闭, 见§6.9深夜复核②)")
    if bad:
        from monitoring.alerts import send_alert
        send_alert(f"配置一致性检查: {bad} 项漂移——引擎/实盘/spec 未对齐, "
                   f"任何引用回测结论前必须先修复(见上方 diff)", level="warning")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
