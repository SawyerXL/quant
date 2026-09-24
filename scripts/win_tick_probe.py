"""QMT 仿真行情探针(2026-09-24 超付机制调查) — 仅 Windows 运行
查指定券的仿真行情源状态: 最新价/卖一/买一/时间戳/成交明细。
判定: 行情源冻结(时间戳陈旧/无卖一) vs 活跃 —— 决定超付是
"柜台按限价成交"还是"冻结源无卖一→按限价"。
用法(Windows): python scripts/win_tick_probe.py 123107 127040 127061 110075
"""
import sys

CODES = sys.argv[1:] if len(sys.argv) > 1 else ["123107", "127040", "127061"]

try:
    from xtquant import xtdata
except ImportError as e:
    print(f"IMPORT_FAIL: {e}")
    sys.exit(1)


def to_sym(c: str) -> str:
    c = c.split(".")[0]
    return f"{c}.{'SH' if c.startswith('11') else 'SZ'}"


for c in CODES:
    sym = to_sym(c)
    print(f"=== {sym} ===")
    try:
        tick = xtdata.get_full_tick([sym])
        t = tick.get(sym, {}) if isinstance(tick, dict) else {}
        if not t:
            print("  空tick(无数据)")
            continue
        for k in ("time", "lastPrice", "lastClose", "askPrice", "bidPrice",
                  "volume", "amount", "open", "high", "low"):
            print(f"  {k}: {t.get(k)}")
        # 买卖五档
        for i in range(5):
            ap = t.get(f"askPrice", t.get(f"ask_price", []))
            if isinstance(ap, (list, tuple)) and len(ap) > i:
                print(f"  卖{i+1}: {ap[i]}")
            else:
                break
    except Exception as e:
        print(f"  错误: {e}")
