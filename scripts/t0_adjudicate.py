"""做T改进裁决回测 (2026-10-10, 真实信号 × 5分钟线路径模拟)。

为什么: 真实成交账裁决不达标(47笔胜率44.7%); 日线回测无法裁定
"先触止损还是先触了结"(G1 两判读跨零轴)——5分钟线可以。对
t_signal_log 全量信号, 在同一条分钟内路径上并排四变体:
  ① 现规则: leg2(±1%了结) → 14:50尾盘了结(当日最后bar收价)
  ② 旧规则: leg2 → 次日开盘强平(对照, 量化旧口径高估/低估)
  ③ +G1止损: leg2 与 -1%止损(正T=卖价×1.01买回/反T=买价×0.99卖出)
     先触先得, 止损含费净-1.26%
  ④ +提前了结: 14:30 / 14:00 收盘价了结
口径: 与 settle_t_signals 同源(_minutes/_legs/_cost/POSITION_FRAC);
14:50了结价=当日最后一根5分钟bar收价(涨跌停价限价单≈即时成交)。
用法: python scripts/t0_adjudicate.py [--rebuild]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from settle_t_signals import _minutes, _legs, _cost, SIGNAL_LOG, _sina_symbol  # noqa: E402

POSITION_FRAC = 1 / 3
STOP = 0.01
TAIL_TIMES = ["14:50", "14:30", "14:00"]


def _tail_price(day, t1, before: str) -> float | None:
    """t1 之后、before 时刻之前最后一根bar的收价≈了结价; 无bar返回None。"""
    seg = day[(day["day"] > t1) & (day["day"].astype(str).str[11:19] < before)]
    if seg.empty:
        return None
    return float(seg.iloc[-1]["close"])


def _next_open(m, d) -> float | None:
    nxt = m[m["day"].astype(str).str[:10] > d]
    return float(nxt.iloc[0]["open"]) if not nxt.empty else None


def adjudicate_one(row, m) -> dict | None:
    code, d, t = str(row["code"]).zfill(6), str(row["date"]), str(row["time"])
    direction = row["direction"]
    l1, l2 = _legs(row)
    if l1 is None:
        return None
    day = m[m["day"].astype(str).str[:10] == d] if not m.empty else pd.DataFrame()
    if day.empty:
        return None  # 日线未覆盖, 与 settle 口径一致跳过
    after = day[day["day"].astype(str).str[11:19] >= t]
    if after.empty:
        return None  # 信号在收盘后
    hit1 = after[after["high"] >= l1] if direction == "正T" else after[after["low"] <= l1]
    if hit1.empty:
        return None  # 首腿未成交(信号未兑现, 与 settle 口径一致不入账)
    t1 = hit1.iloc[0]["day"]

    def net(exit_px):
        if direction == "正T":
            sell_px, buy_px = l1, exit_px
        else:
            buy_px, sell_px = l1, exit_px
        return (sell_px - buy_px) / buy_px * 100 - _cost(code)

    # leg2: 只在 leg1 之后找
    rest = day[day["day"] > t1]
    hit2 = rest[rest["low"] <= l2] if direction == "正T" else rest[rest["high"] >= l2]

    out = {"code": code, "date": d, "direction": direction, "l1": l1}
    if not hit2.empty:
        out["leg2"] = round(net(l2), 4)
    else:
        out["leg2"] = None

    # ① 14:50 尾盘(现规则)
    tp50 = _tail_price(day, t1, "14:50")
    out["tail1450"] = round(net(tp50), 4) if tp50 else None
    # ④ 提前 14:30 / 14:00
    tp30 = _tail_price(day, t1, "14:30")
    tp00 = _tail_price(day, t1, "14:00")
    out["tail1430"] = round(net(tp30), 4) if tp30 else None
    out["tail1400"] = round(net(tp00), 4) if tp00 else None
    # ② 次日开盘(旧规则对照)
    no = _next_open(m, d)
    out["next_open"] = round(net(no), 4) if no else None

    # ③ G1 止损: leg2 与止损先触先得
    stop_px = l1 * (1 + STOP) if direction == "正T" else l1 * (1 - STOP)
    hit_stop = rest[rest["high"] >= stop_px] if direction == "正T" else rest[rest["low"] <= stop_px]
    if not hit2.empty and not hit_stop.empty:
        first = "leg2" if hit2.iloc[0]["day"] <= hit_stop.iloc[0]["day"] else "stop"
    elif not hit2.empty:
        first = "leg2"
    elif not hit_stop.empty:
        first = "stop"
    else:
        first = "tail"  # 都没触到 → 走尾盘(与现规则同)
    if first == "leg2":
        out["g1"] = out["leg2"]
    elif first == "stop":
        out["g1"] = round(net(stop_px), 4)
    else:
        out["g1"] = out["tail1450"]
    return out


def _report(name, vals):
    v = np.array([x for x in vals if x is not None and not np.isnan(x)], dtype=float)
    if len(v) == 0:
        print(f"{name:<16} 无样本")
        return
    wins = (v > 0).sum()
    print(f"{name:<16} N={len(v):>4} 胜率{wins/len(v):>6.1%} 单笔均值{v.mean():>+8.4f}%"
          f"(leg) {v.mean()*POSITION_FRAC:>+8.4f}%(仓位) 合计{v.sum():>+8.2f}%(leg)")


def main():
    if not SIGNAL_LOG.exists():
        print("无信号日志")
        return
    sig = pd.read_csv(SIGNAL_LOG, dtype={"code": str})
    codes = sorted(sig["code"].unique())
    print(f"信号 {len(sig)} 条, {len(codes)} 只票, 拉取5分钟线...")
    minutes = {c: _minutes(str(c).zfill(6)) for c in codes}
    print("路径模拟中...")

    results = []
    for _, r in sig.iterrows():
        c = str(r["code"]).zfill(6)
        res = adjudicate_one(r, minutes.get(c))
        if res is not None:
            results.append(res)
    print(f"可裁决信号 {len(results)} 条 (跳过: 首腿未成交/无行情/价格缺失)\n")

    df = pd.DataFrame(results)
    # 裁决列: leg2 命中→leg2值; 否则→该变体的了结价路径
    # 注意: dict→DataFrame 时 None 已变 NaN, 判空必须用 isna 而非 is not None
    for col, tail in [("tail1450", "tail1450"), ("tail1430", "tail1430"),
                      ("tail1400", "tail1400"), ("next_open", "next_open")]:
        df[col + "_v"] = df.apply(
            lambda r: r[tail] if pd.isna(r["leg2"]) else r["leg2"], axis=1)
    df["g1_v"] = df["g1"]

    print("=" * 66)
    print("全信号口径:")
    _report("leg2命中(纯了结)", df["leg2"])
    _report("①现规则14:50", df["tail1450_v"])
    _report("④提前14:30", df["tail1430_v"])
    _report("④提前14:00", df["tail1400_v"])
    _report("②旧规则次日开", df["next_open_v"])
    _report("③G1止损", df["g1_v"])

    for d_ in ["正T", "反T"]:
        sub = df[df["direction"] == d_]
        print(f"\n── {d_} (N={len(sub)}) ──")
        _report("①现规则14:50", sub["tail1450_v"])
        _report("③G1止损", sub["g1_v"])
        _report("④提前14:30", sub["tail1430_v"])

    # 09-03 修订后子集
    sub2 = df[df["date"] >= "2026-09-03"]
    print(f"\n── 09-03修订后 (N={len(sub2)}) ──")
    _report("①现规则14:50", sub2["tail1450_v"])
    _report("③G1止损", sub2["g1_v"])
    _report("④提前14:30", sub2["tail1430_v"])

    df.to_csv("logs/t0_adjudicate_20261010.csv", index=False)
    print("\n产出: logs/t0_adjudicate_20261010.csv")


if __name__ == "__main__":
    main()
