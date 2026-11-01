"""预测前的消息面完整性检查（2026-11-01 漏检后新增）。

为什么：11-01 15:12 的央行/证监会座谈会已在 news_feed 里，但分析时
只看了 10-31 的事件文件 + news_feed 尾部 15 行；11-01 事件台账尚未生成，
且旧分类器被标题里的“证券公司/基金公司”触发 EXCLUDE“公司”判 material=0。
本脚本对指定日期（可 lookback）做全量扫描，输出 material 与政策会议
关键词命中；**没有 material 也必须写 no_material_event**。

用法：python scripts/news_precheck.py --date 2026-11-01
输出：logs/news_precheck_YYYYMMDD.json/.md；只读，不改预测/信号。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import news_event_ledger as nel

FEED = ROOT / "logs" / "news_feed.csv"
OUT_DIR = ROOT / "logs"
POLICY_PAT = ("座谈会", "召开会议", "资本市场工具", "回购增持再贷款",
              "央行", "人民银行", "证监会", "金融监管总局")


def scan(days: list[str]) -> dict:
    rows = []
    n_feed_rows = 0
    feed_latest = ""
    if FEED.exists():
        with FEED.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pt = str(r.get("pub_time", ""))
                if pt:
                    feed_latest = max(feed_latest, pt)
                if pt[:10] not in days:
                    continue
                n_feed_rows += 1
                title = (r.get("title") or "").strip()
                if not title:
                    continue
                cat, hits = nel._classify(title)
                if not cat:
                    continue
                mat = bool(nel._material(title, cat))
                policy_hit = any(x in title for x in POLICY_PAT)
                if mat or policy_hit:
                    rows.append({
                        "pub_time": pt, "source": r.get("source", ""),
                        "category": cat, "material": int(mat),
                        "policy_hit": int(policy_hit),
                        "keywords": ",".join(hits),
                        "title": title[:300],
                    })
    rows.sort(key=lambda x: x["pub_time"])
    material = [r for r in rows if r["material"]]
    now = datetime.now()
    stale_h = None
    if feed_latest:
        try:
            stale_h = round((now - datetime.strptime(
                feed_latest[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600, 2)
        except ValueError:
            stale_h = None
    return {
        "days": days,
        "n_feed_rows": n_feed_rows,
        "n_scanned": len(rows),
        "n_material": len(material),
        "n_policy_hit": sum(r["policy_hit"] for r in rows),
        "material": material,
        "policy_hits": [r for r in rows if r["policy_hit"]],
        "no_material_event": len(material) == 0,
        "feed_latest": feed_latest,
        "feed_stale_hours": stale_h,
        "feed_stale": bool(stale_h is not None and stale_h > 12),
        "no_data": n_feed_rows == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--lookback", type=int, default=1,
                    help="同时扫描前 N 个自然日（默认 1）")
    args = ap.parse_args()
    d0 = datetime.strptime(args.date, "%Y-%m-%d").date()
    days = [(d0 - timedelta(days=i)).isoformat()
            for i in range(args.lookback + 1)]
    out = scan(days)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"news_precheck_{d0.strftime('%Y%m%d')}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        f"# 消息面预检 {args.date}（lookback={args.lookback}）",
        "",
        f"- 扫描日：{', '.join(days)}；feed行：{out['n_feed_rows']}；"
        f"候选/关键词命中：{out['n_scanned']}；"
        f"material=1：{out['n_material']}；policy_hit：{out['n_policy_hit']}；"
        f"feed_latest={out['feed_latest']}；"
        f"stale={out['feed_stale_hours']}h。",
    ]
    if out["no_data"]:
        lines.append("- **NO_DATA**：目标窗口 feed 无行，不能声称“无事件”。")
    elif out["feed_stale"]:
        lines.append("- **FEED_STALE**：feed 超过 12h 未更新，不能声称“无事件”。")
    if out["no_material_event"]:
        lines.append("- **no_material_event**：未发现重大国内政策事件。")
    else:
        lines += ["", "| 时间 | 类别 | material | policy_hit | 标题 |",
                  "|---|---|---:|---:|---|"]
        for r in out["material"]:
            lines.append(f"| {r['pub_time']} | {r['category']} | "
                         f"{r['material']} | {r['policy_hit']} | "
                         f"{r['title'][:100]} |")
    if out["policy_hits"]:
        lines += ["", "**政策关键词命中（含 material=0，需人工复核）**："]
        for r in out["policy_hits"]:
            lines.append(f"- `{r['pub_time']}` [{r['category']}] "
                         f"material={r['material']} {r['title'][:160]}")
    lines += ["", f"JSON: `{p}`"]
    md = OUT_DIR / f"news_precheck_{d0.strftime('%Y%m%d')}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
