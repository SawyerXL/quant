"""委托日志“按委托日归集 + 去重”（2026-10-25）。

为什么：`logs/qmt_orders_*.json` 是**累积账户委托**，且 `signal_date` 用
导出日（节假日也导）；同一批 09-30 的 568 条 CB 测试单被 10-01/02/05/07
重复导出，直接按文件日统计会把 1823 条算成“节假日废单”。

口径：
- 真实日期 = `order_time`（epoch 秒，+08）的日期，不用 signal_date；
- 去重键 = (code, direction, shares, limit_price, order_time) 的 hash；
  同一批重导只算一次；order_id 仅作信息；
- 单日 >100 条标 `bulk_test`（全宇宙/连通性测试，不是策略单）。
输出：logs/order_daily_audit_20261025.json + 打印。
"""
import glob
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "logs" / "order_daily_audit_20261025.json"
TZ = timezone(timedelta(hours=8))


def key_of(o):
    s = f"{o.get('code')}|{o.get('direction')}|{o.get('shares')}|" \
        f"{o.get('limit_price')}|{o.get('order_time')}"
    return hashlib.md5(s.encode()).hexdigest()


def main():
    files = sorted(glob.glob(str(ROOT / "logs" / "*orders_*.json")))
    seen = {}
    for p in files:
        d = json.load(open(p))
        for o in d.get("orders", []):
            k = key_of(o)
            if k not in seen:
                seen[k] = o
    by_date = defaultdict(list)
    for o in seen.values():
        try:
            dt = datetime.fromtimestamp(int(o["order_time"]), tz=TZ).strftime("%Y-%m-%d")
        except Exception:
            dt = "unknown"
        by_date[dt].append(o)
    rows = {}
    for dt, os_ in sorted(by_date.items()):
        c = Counter(str(o.get("status")) for o in os_)
        rows[dt] = {
            "n": len(os_), "status": dict(c),
            "bulk_test": len(os_) > 100,
            "remarks": Counter(o.get("remark", "") for o in os_).most_common(3),
        }
    out = {
        "files": len(files),
        "unique_orders": len(seen),
        "duplicates_removed": sum(len(json.load(open(p)).get("orders", [])) for p in files) - len(seen),
        "by_order_date": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
