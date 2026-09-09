"""
pTrade 仿真对账 — 比对 pTrade 策略 SIGNAL 行与 Linux 信号 JSON（回测复验纪律的移植版）。

用法:
  # 日常对账: 把 pTrade 平台日志(含 SIGNAL 行)存成文件后比对当日
  python scripts/reconcile_ptrade.py --ptrade logs/ptrade_signal_dump.txt

  # 归档当前 Linux 信号后再比对（建议每天收盘后跑一次, 攒历史）
  python scripts/reconcile_ptrade.py --ptrade logs/ptrade_signal_dump.txt --archive

  # 只比最近 N 天归档
  python scripts/reconcile_ptrade.py --ptrade logs/ptrade_signal_dump.txt --days 10

  # 自测
  python scripts/reconcile_ptrade.py --selftest

pTrade 侧解析规则: 从任意日志行中定位 "SIGNAL|" 段:
  SIGNAL|<日期>|ratio=0.70|rebal=False|halt=False|selected=a,b|sell=x|buy=y|note=...
  同日多条取最后一条。

差异分级:
  PASS  — 仓位档/目标组合/买卖清单全部一致
  WARN  — 已知允许差异(真实价过滤/盘中bar口径, 每侧≤2只) 或 非调仓日MA10出清等需人工确认项
  BLOCK — 仓位档不一致 / 组合差异>2只 → 移植失败, 禁止进入下一阶段

已知口径差异(文档备案于 docs/ptrade_migration_plan.md §3.2-3.3):
  1. 买得起过滤/股数: pTrade 用 snapshot 真实价, Linux 用前复权收盘价 → 边缘±1~2只
  2. 排名/波动率: pTrade 用 T-1 完整K线, Linux 14:25 跑可能含当日盘中bar
  3. MA10 天数: Linux=3d vs pTrade=4d(决策点4未对齐前, 出清名单会分叉)
"""
import argparse
import json
import re
import sys
from pathlib import Path

SIGNAL_FILE = Path("data_store/meta/signal_a_latest.json")
ARCHIVE_DIR = Path("logs/signal_archive")
ALLOWED_SET_DIFF = 2      # 已知允许差异上限: 每侧集合差 ≤2 只

OK, WARN, BLOCK = "OK", "WARN", "BLOCK"


# ── pTrade SIGNAL 行解析 ──────────────────────────────────────────
def parse_ptrade_line(line: str):
    """从任意日志行提取 SIGNAL 段 → dict。不是 SIGNAL 行返回 None。"""
    idx = line.find("SIGNAL|")
    if idx < 0:
        return None
    parts = line[idx:].rstrip("\n").split("|")
    if len(parts) < 8:
        return None
    sig = {"date": parts[1]}
    for kv in parts[2:]:
        k, _, v = kv.partition("=")
        sig[k] = v
    try:
        sig["ratio"] = float(sig.get("ratio"))
    except (TypeError, ValueError):
        sig["ratio"] = None
    sig["rebal"] = sig.get("rebal") == "True"
    sig["halt"] = sig.get("halt") == "True"
    sig["selected"] = [c for c in sig.get("selected", "").split(",") if c]
    sig["sell"] = [c for c in sig.get("sell", "").split(",") if c]
    sig["buy"] = [c for c in sig.get("buy", "").split(",") if c]
    return sig


def parse_ptrade_text(text: str) -> dict:
    """多行日志 → {日期: signal}，同日多条取最后一条"""
    out = {}
    for line in text.splitlines():
        sig = parse_ptrade_line(line)
        if sig:
            out[sig["date"]] = sig
    return out


# ── Linux 信号加载 ────────────────────────────────────────────────
def load_linux_signal(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("holdings", [])
    d.setdefault("buy", [])
    d.setdefault("sell", [])
    return d


def archive_signal() -> Path:
    """把当日 signal_a_latest.json 归档为 signal_<signal_date>.json，攒对账历史"""
    d = load_linux_signal(SIGNAL_FILE)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"signal_{d['signal_date']}.json"
    dest.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已归档: {dest}")
    return dest


def load_linux_archive(days: int | None = None) -> dict:
    """归档目录 → {日期: signal}（按 JSON 内 signal_date 为准）"""
    if not ARCHIVE_DIR.exists():
        return {}
    files = sorted(ARCHIVE_DIR.glob("signal_*.json"))
    if days:
        files = files[-days:]
    out = {}
    for f in files:
        try:
            d = load_linux_signal(f)
            out[d.get("signal_date")] = d
        except Exception:
            continue
    return out


# ── 比对 ──────────────────────────────────────────────────────────
def compare(linux: dict, ptrade: dict) -> list[tuple[str, str]]:
    res = []
    date = linux.get("signal_date") or ptrade.get("date")

    # 1) 仓位档: 不一致 = 择时错位, 直接阻断
    r1, r2 = linux.get("position_ratio"), ptrade.get("ratio")
    if r1 is None or r2 is None:
        res.append((BLOCK, f"仓位档缺失: Linux={r1} pTrade={r2}"))
    elif abs(float(r1) - float(r2)) > 1e-6:
        res.append((BLOCK, f"仓位档不一致: Linux={r1:.0%} pTrade={r2:.0%}"))
    else:
        res.append((OK, f"仓位档一致 {r1:.0%}"))

    # 2) 目标组合集合
    h1, h2 = set(linux.get("holdings", [])), set(ptrade.get("selected", []))
    only_l = sorted(h1 - h2)     # Linux 有 pTrade 无
    only_p = sorted(h2 - h1)     # pTrade 有 Linux 无
    if not ptrade.get("rebal") and not h2:
        # DRY_RUN 起步期: pTrade 账户无持仓, 非调仓日 selected 天然为空 → 只比仓位档
        res.append((OK, f"非调仓日且pTrade侧无持仓, 组合比对跳过(Linux持仓{len(h1)}只)"))
    elif not only_l and not only_p:
        res.append((OK, f"目标持仓一致 {len(h1)}只"))
    elif len(only_l) <= ALLOWED_SET_DIFF and len(only_p) <= ALLOWED_SET_DIFF:
        res.append((WARN, f"组合边缘差异: Linux独有{only_l} pTrade独有{only_p} "
                          f"(已知允许: 真实价过滤/盘中bar口径, 若涉及MA10出清则指向决策点4)"))
    else:
        res.append((BLOCK, f"组合差异过大: Linux独有{len(only_l)}只{only_l[:5]}... "
                           f"pTrade独有{len(only_p)}只{only_p[:5]}..."))

    # 3) 调仓日: 买卖清单必须对齐
    if ptrade.get("rebal"):
        for key, label in (("sell", "卖出"), ("buy", "买入")):
            a, b = set(linux.get(key, [])), set(ptrade.get(key, []))
            oa, ob = sorted(a - b), sorted(b - a)
            if not oa and not ob:
                res.append((OK, f"{label}清单一致 {len(a)}只"))
            elif len(oa) <= ALLOWED_SET_DIFF and len(ob) <= ALLOWED_SET_DIFF:
                res.append((WARN, f"{label}清单边缘差异: Linux独有{oa} pTrade独有{ob}"))
            else:
                res.append((BLOCK, f"{label}清单差异过大: Linux独有{oa[:5]}... pTrade独有{ob[:5]}..."))
    elif ptrade.get("sell") or ptrade.get("buy"):
        res.append((WARN, f"非调仓日 pTrade 有买卖(卖{ptrade['sell']} 买{ptrade['buy']}) — "
                          f"核对是否为 MA10 出清, 并查 Linux 当日日志确认"))

    # 4) 状态提示
    if ptrade.get("halt"):
        res.append((WARN, "pTrade 熔断B方案生效中, 两边行为分叉属正常"))
    if ptrade.get("note"):
        res.append((OK, f"pTrade备注: {ptrade['note'][:80]}"))
    return res


def verdict(results: list[tuple[str, str]]) -> str:
    if any(lv == BLOCK for lv, _ in results):
        return "FAIL"
    if any(lv == WARN for lv, _ in results):
        return "WARN"
    return "PASS"


# ── 自测 ──────────────────────────────────────────────────────────
def _selftest() -> bool:
    line = ('2026-09-01 14:40:05,INFO,SIGNAL|2026-09-01|ratio=0.70|rebal=False|halt=False|'
            'selected=000725,000988,002008|sell=|buy=|note=测试')
    sig = parse_ptrade_line(line)
    assert sig["date"] == "2026-09-01" and sig["ratio"] == 0.70
    assert sig["selected"] == ["000725", "000988", "002008"]
    assert sig["sell"] == [] and sig["buy"] == []
    assert sig["rebal"] is False and sig["halt"] is False

    base = {"signal_date": "2026-09-01", "position_ratio": 0.70,
            "holdings": ["000725", "000988", "002008"], "buy": [], "sell": []}

    # 全一致 → PASS
    assert verdict(compare(base, sig)) == "PASS"
    # 仓位档错 → FAIL
    bad_r = dict(sig, ratio=0.50)
    assert verdict(compare(base, bad_r)) == "FAIL"
    # 边缘差异 ≤2 → WARN
    edge = dict(base, holdings=["000725", "000988", "002008", "002009"])
    assert verdict(compare(edge, sig)) == "WARN"
    # 大面积差异 → FAIL
    big = dict(base, holdings=["300001", "300002", "300003", "300004", "300005"])
    assert verdict(compare(big, sig)) == "FAIL"
    # 调仓日买卖清单对齐
    rebal = dict(sig, rebal=True, sell=["002008"], buy=["002009"])
    rb_base = dict(base, buy=["002009"], sell=["002008"])
    assert verdict(compare(rb_base, rebal)) == "PASS"
    print("自测全部通过 ✓")
    return True


def main():
    ap = argparse.ArgumentParser(description="pTrade 仿真对账")
    ap.add_argument("--ptrade", help="pTrade 日志文件路径, '-' 表示从 stdin 读")
    ap.add_argument("--signal", default=str(SIGNAL_FILE), help="Linux 信号 JSON(默认 signal_a_latest.json)")
    ap.add_argument("--days", type=int, default=None, help="只比对最近 N 天归档")
    ap.add_argument("--archive", action="store_true", help="先归档当前 Linux 信号再比对")
    ap.add_argument("--selftest", action="store_true", help="自测后退出")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if not args.ptrade:
        ap.error("需要 --ptrade <日志文件> 或 --selftest")

    if args.ptrade == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.ptrade).read_text(encoding="utf-8", errors="replace")
    ptrade_map = parse_ptrade_text(text)
    if not ptrade_map:
        print("✗ 未解析到任何 SIGNAL 行, 请确认日志内容"); sys.exit(2)

    linux_map = load_linux_archive(args.days)
    if args.archive:
        archive_signal()
        linux_map = load_linux_archive(args.days)
    # 当日信号也纳入(归档里可能已含同日, 以归档为准)
    try:
        cur = load_linux_signal(Path(args.signal))
        linux_map.setdefault(cur.get("signal_date"), cur)
    except Exception:
        pass
    if not linux_map:
        print("✗ Linux 侧无信号数据"); sys.exit(2)

    exit_code = 0
    dates = sorted(set(ptrade_map) & set(linux_map))
    missing_l = sorted(set(ptrade_map) - set(linux_map))
    missing_p = sorted(set(linux_map) - set(ptrade_map))
    for date in dates:
        print(f"\n═══ 对账 {date} ═══")
        res = compare(linux_map[date], ptrade_map[date])
        for lv, msg in res:
            icon = {"OK": "  ✓", "WARN": "  △", "BLOCK": "  ✗"}[lv]
            print(f"{icon} [{lv}] {msg}")
        v = verdict(res)
        print(f"  → 结论: {v}")
        if v == "FAIL":
            exit_code = 2
        elif v == "WARN" and exit_code == 0:
            exit_code = 1
    if missing_l:
        print(f"\n△ pTrade 有但 Linux 缺归档: {missing_l} (检查信号是否生成)")
        exit_code = max(exit_code, 1)
    if missing_p:
        print(f"△ Linux 有但 pTrade 缺日志: {missing_p} (检查策略是否运行)")
        exit_code = max(exit_code, 1)
    if not dates:
        print("✗ 两边无同一天的数据可比对")
        sys.exit(2)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
