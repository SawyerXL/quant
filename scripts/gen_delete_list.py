"""删笔清单生成器(十三轮: 清单由执行记录删笔条目生成, 不手工录入)。

输入 record.json = [{"code": "127038", "direction": "buy", "reason": "..."}]
输出 logs/manual_delete_list.json = {"valid_until": ..., "entries": [...补deleted_at]}
用法:
  python scripts/gen_delete_list.py --deletions logs/dels_20261019.json \
      --valid-until 2026-10-29 --out logs/manual_delete_list.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from execution.manual_delete_list import generate_from_deletions  # noqa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deletions", required=True, help="删笔记录 JSON(考试收盘判定产物)")
    ap.add_argument("--valid-until", required=True, help="有效期(下个调仓日, YYYY-MM-DD)")
    ap.add_argument("--out", default="logs/manual_delete_list.json")
    args = ap.parse_args()

    deletions = json.loads(Path(args.deletions).read_text(encoding="utf-8"))
    dl = generate_from_deletions(deletions, args.valid_until)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dl, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"清单已生成: {out} ({len(dl['entries'])} 条, 有效期至 {args.valid_until})")


if __name__ == "__main__":
    main()
