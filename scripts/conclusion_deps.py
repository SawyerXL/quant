"""
结论-数据依赖自动化（2026-09-13 晚落成，overnight_open_questions.md
"结论-数据依赖表立项"条款；2026-09-13 晚用户裁定两处硬化）。

把手工做了七八遍的事变成机器动作：结案结论记录自己依赖的
(股票集合, 日期区间)，写库操作输出修改范围，交集非空 → 结论自动标
"需复核"。背景：−0.63 vs −1.93 归属案里，"哪些结论被这次写库动过"
全靠人肉追，每一次修复/重拉都要手工重标一遍待确认清单。

硬化一（静默失效与正常同形，四次事故同根因）：
  try/except 保护主链是对的，但异常必须落心跳文件而非只落 except 分支。
  {workdir}/conclusion_deps_state.json 记录 last_error；健康行读 health()
  输出 dep_scan 字段——字段缺失或 last_error 非空 = 告警（dead-man）。

硬化二（自测不得污染生产工件）：
  所有路径受 --workdir / 环境变量 CONCLUSION_DEPS_WORKDIR 控制，
  默认 logs/。自测一律用独立目录；生产路径只接受真实写库事件。

三件套 + 心跳：
  表   {workdir}/conclusion_dependencies.json   结论 → deps[{stocks,start,end}]
  范围 {workdir}/write_scopes.jsonl            写库操作的修改范围(append-only)
  状态 {workdir}/conclusion_review_state.json  check 产物(幂等重算, 可覆盖)
  心跳 {workdir}/conclusion_deps_state.json    dep_scan 供健康行

股票集通配: "ALL"=全市场个股价量单元 | "INDEX"=择时基准指数
           | "MEMBERS"=CSI800 成分快照表——三种不同数据对象互不包含。
区间为闭区间, end=null=开放至今。kind: append=只加新日期 | rewrite=历史日期被写。

用法:
  python scripts/conclusion_deps.py record --source 名 --stocks ALL \
      --start 2026-09-14 --end 2026-09-14 --kind rewrite [--note ...] [--workdir ...]
  python scripts/conclusion_deps.py check [--since ...] [--workdir ...]
  python scripts/conclusion_deps.py list [--workdir ...]
  python scripts/conclusion_deps.py health [--workdir ...]
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_WORKDIR = Path(__file__).parent.parent / "logs"


def _workdir(workdir=None):
    return Path(workdir or os.environ.get("CONCLUSION_DEPS_WORKDIR")
                or DEFAULT_WORKDIR)


def _paths(workdir=None):
    wd = _workdir(workdir)
    return {"table": wd / "conclusion_dependencies.json",
            "scopes": wd / "write_scopes.jsonl",
            "state": wd / "conclusion_review_state.json",
            "heartbeat": wd / "conclusion_deps_state.json"}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_last_error(wd, msg):
    """心跳里的错误标记。自身不抛——连心跳都写不了时该链路已不可救,
    健康行会以"心跳文件缺失"告警。"""
    try:
        p = wd / "conclusion_deps_state.json"
        s = {}
        if p.exists():
            s = json.loads(p.read_text(encoding="utf-8"))
        s["last_error"] = f"{_now()} {msg}"
        p.write_text(json.dumps(s, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception:
        pass


def _bump_heartbeat(wd, **fields):
    """成功记录一次范围 = 管线活着, 清 last_error。自身不抛。"""
    try:
        p = wd / "conclusion_deps_state.json"
        s = {}
        if p.exists():
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                s = {}
        s.update(fields)
        s["last_record_ts"] = _now()
        s["ranges_total"] = int(s.get("ranges_total", 0)) + 1
        s["last_error"] = None
        p.write_text(json.dumps(s, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception:
        pass


def record_scope(source, stocks, start, end, kind, note="", workdir=None):
    """写库操作登记一次修改范围。永不抛异常——监控件不打断被监控的主链
    (daily_data_update 挂钩点依赖此保证)；异常落心跳 last_error。"""
    wd = _workdir(workdir)
    paths = _paths(wd)
    try:
        if isinstance(stocks, str):
            stocks = [stocks]
        rec = {"ts": _now(), "source": source,
               "stocks": sorted({str(s) for s in stocks}),
               "start": start, "end": end, "kind": kind, "note": note}
        paths["scopes"].parent.mkdir(parents=True, exist_ok=True)
        with open(paths["scopes"], "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _set_last_error(wd, f"scope append 失败: {e}")
        return None
    _bump_heartbeat(wd)
    return rec


TOKENS = ("ALL", "INDEX", "MEMBERS", "CB")


def _stocks_intersect(dep_stocks, scope_stocks):
    """三个通配是三种不同数据对象(个股价量单元/指数序列/成分快照表),
    不互相包含: 写全市场个股不影响 MEMBERS 表, 写 INDEX 不影响个股结论。
    具体代码(600519 等)∈"ALL"域, 与 ALL 通配互配。"""
    if "ALL" in dep_stocks:
        return "ALL" in scope_stocks or any(c not in TOKENS for c in scope_stocks)
    if "ALL" in scope_stocks:
        return "ALL" in dep_stocks or any(c not in TOKENS for c in dep_stocks)
    if any(t in dep_stocks for t in ("INDEX", "MEMBERS")):
        return bool(set(dep_stocks) & set(scope_stocks) & set(TOKENS))
    return bool(set(dep_stocks) & set(scope_stocks))


def _dates_overlap(dep_start, dep_end, scope_start, scope_end):
    lo = max(dep_start or "0000-00-00", scope_start or "0000-00-00")
    hi = min(dep_end or "9999-99-99", scope_end or "9999-99-99")
    return lo <= hi


def load_table(workdir=None):
    p = _paths(workdir)["table"]
    if not p.exists():
        return {"conclusions": []}
    return json.loads(p.read_text(encoding="utf-8"))


def load_scopes(since, workdir=None):
    p = _paths(workdir)["scopes"]
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if since and rec.get("ts", "")[:10] < since:
            continue
        out.append(rec)
    return out


def check(since=None, workdir=None):
    """对 since(默认=表注册日)以来的写库范围与结论依赖取交集,
    全量重算 review_state(幂等)。返回 {id: {...}}。"""
    wd = _workdir(workdir)
    paths = _paths(wd)
    table = load_table(wd)
    since = since or table.get("registered_at")
    scopes = load_scopes(since, wd)
    state = {}
    for c in table.get("conclusions", []):
        reasons = []
        for dep in c.get("deps", []):
            for s in scopes:
                if _stocks_intersect(dep.get("stocks", []), s.get("stocks", [])) \
                        and _dates_overlap(dep.get("start"), dep.get("end"),
                                           s.get("start"), s.get("end")):
                    reasons.append({
                        "scope_ts": s["ts"], "source": s["source"],
                        "kind": s.get("kind", ""),
                        "scope_range": [s.get("start"), s.get("end")],
                        "dep": dep.get("note", "")})
        state[c["id"]] = {
            "needs_review": bool(reasons),
            "reasons": reasons,
            "checked_at": _now(),
        }
    paths["state"].parent.mkdir(parents=True, exist_ok=True)
    paths["state"].write_text(json.dumps(state, ensure_ascii=False, indent=2))
    # 心跳: check 产物供健康行汇报 flagged 数
    hb = paths["heartbeat"]
    hs = {}
    if hb.exists():
        try:
            hs = json.loads(hb.read_text(encoding="utf-8"))
        except Exception:
            hs = {}
    hs["last_check_ts"] = _now()
    hs["flagged_total"] = sum(1 for v in state.values() if v["needs_review"])
    hb.write_text(json.dumps(hs, ensure_ascii=False, indent=1), encoding="utf-8")
    return state


# 首次安装宽限(2026-09-13晚用户裁定): 心跳缺失在合法运行之前不该告警——
# 至 T+1 首次日更(周一 17:00 日更的 record_scope 生成心跳)前状态=bootstrap
# 不告警; 宽限到期仍缺失 → missing 告警。宽限期本身写进健康行, 不隐式。
BOOTSTRAP_UNTIL = "2026-09-14 18:00"


def health(workdir=None):
    """dep_scan 字段供 daily_health_line 嵌入。bootstrap/ok 不告警,
    missing/err 告警(心跳文件不存在 = 管线未接或从未成功过一次记录)。"""
    p = _paths(workdir)["heartbeat"]
    if not p.exists():
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if now < BOOTSTRAP_UNTIL:
            return {"dep_scan": "bootstrap",
                    "bootstrap_until": BOOTSTRAP_UNTIL,
                    "ranges": 0, "flagged": 0, "last_error": None}
        return {"dep_scan": "missing", "ranges": 0, "flagged": 0,
                "last_error": "心跳文件不存在(宽限已到期)"}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"dep_scan": "err", "ranges": 0, "flagged": 0,
                "last_error": f"心跳不可读: {e}"}
    return {"dep_scan": "err" if s.get("last_error") else "ok",
            "ranges": s.get("ranges_total", 0),
            "flagged": s.get("flagged_total", 0),
            "last_record_ts": s.get("last_record_ts"),
            "last_check_ts": s.get("last_check_ts"),
            "last_error": s.get("last_error")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_wd(p):
        p.add_argument("--workdir", default=None,
                       help="工件目录(默认 logs/; 自测必须用独立目录)")

    r = sub.add_parser("record", help="登记一次写库修改范围")
    add_wd(r)
    r.add_argument("--source", required=True)
    r.add_argument("--stocks", default="ALL",
                   help="逗号分隔代码, 或 ALL/INDEX/MEMBERS 通配")
    r.add_argument("--stocks-file", default=None,
                   help="JSON数组文件(代码过多时替代--stocks)")
    r.add_argument("--start", required=True, help="YYYY-MM-DD")
    r.add_argument("--end", required=True, help="YYYY-MM-DD")
    r.add_argument("--kind", required=True, choices=["append", "rewrite"])
    r.add_argument("--note", default="")

    c = sub.add_parser("check", help="范围×依赖交集 → 自动标需复核")
    add_wd(c)
    c.add_argument("--since", default=None, help="YYYY-MM-DD, 默认=表注册日")

    l = sub.add_parser("list", help="列出结论表与当前复核状态")
    add_wd(l)
    h = sub.add_parser("health", help="输出 dep_scan 心跳字段")
    add_wd(h)

    args = ap.parse_args()
    if args.cmd == "record":
        stocks = args.stocks.split(",")
        if args.stocks_file:
            stocks = json.loads(Path(args.stocks_file).read_text(encoding="utf-8"))
        rec = record_scope(args.source, stocks, args.start, args.end,
                           args.kind, args.note, args.workdir)
        print(f"已登记: {json.dumps(rec, ensure_ascii=False)}")
    elif args.cmd == "check":
        state = check(args.since, args.workdir)
        flagged = {k: v for k, v in state.items() if v["needs_review"]}
        for k, v in state.items():
            mark = "🔴需复核" if v["needs_review"] else "✓"
            print(f"{mark} {k}: {len(v['reasons'])} 条命中")
            for r_ in v["reasons"]:
                print(f"   {r_['scope_ts']} {r_['source']} "
                      f"{r_['kind']} {r_['scope_range']} | {r_['dep']}")
        print(f"\n{len(flagged)}/{len(state)} 条结论需复核 → "
              f"{_paths(args.workdir)['state']}")
    elif args.cmd == "list":
        table = load_table(args.workdir)
        state_path = _paths(args.workdir)["state"]
        state = {}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        for c in table.get("conclusions", []):
            st = state.get(c["id"], {})
            mark = "🔴" if st.get("needs_review") else ("✓" if st else "·")
            print(f"{mark} {c['id']} [{c.get('status','')}] {c['title']}")
    elif args.cmd == "health":
        print(json.dumps(health(args.workdir), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
