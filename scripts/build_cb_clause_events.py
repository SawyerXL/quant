"""构建 CB 下修条款事件集 (2026-10-19, 事件研究数据层).

为什么: 预注册要求事件集"非条件化于最终下修"——必须从全量公告取所有
董事会提议/预计触发, 不能用 G2 已完成下修反推。本脚本把原始公告 JSONL
固化为可复跑的事件 parquet。

输入:
  logs/cb_revision_announcements_20261019.jsonl
  logs/cb_revision_announcements_remaining_20261019.jsonl
  data_store/meta/cb_universe.parquet
  logs/cb_convert_price_events.parquet (仅券名映射)
输出:
  data_store/meta/cb_clause_events_{proposal,trigger,not_revise}.parquet
规则:
  proposal=标题含"董事会提议"+"向下修正"; trigger=含"预计触发"+"向下修正";
  not_revise=含"不向下修正"; 同券 proposal 60天/trigger 20天去重;
  2019-01-01~2026-09-30 (数据覆盖边界, 非收益选择).
用法: python scripts/build_cb_clause_events.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

ANN_FILES = [
    Path("logs/cb_revision_announcements_20261019.jsonl"),
    Path("logs/cb_revision_announcements_remaining_20261019.jsonl"),
]
START, END = "2019-01-01", "2026-09-30"


def load_ann():
    raw = {}
    for p in ANN_FILES:
        with open(p, encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                rows = o.get("rows") or []
                if isinstance(rows, dict):
                    rows = [rows] if rows else []
                raw.setdefault(str(o["symbol"]), []).extend(rows)
    out = {}
    for k, v in raw.items():
        df = pd.DataFrame(v)
        if df.empty:
            out[k] = df
            continue
        df["ann_date"] = pd.to_datetime(df["ann_date"].astype(str), format="%Y%m%d", errors="coerce")
        out[k] = df.drop_duplicates(["title", "ann_date"]).sort_values("ann_date")
    return out


def name_map():
    u = pd.read_parquet("data_store/meta/cb_universe.parquet")
    u["code"] = u["code"].astype(str).str.zfill(6)
    u["stock_code"] = u["stock_code"].astype(str).str[:6]
    m = {}
    for _, r in u.iterrows():
        if isinstance(r["name"], str) and r["name"]:
            m.setdefault(r["stock_code"], []).append((r["name"], r["code"]))
    # 用 G2 事件表补老券名称
    try:
        g = pd.read_parquet("logs/cb_convert_price_events.parquet")
        g["code"] = g["code"].astype(str).str.zfill(6)
        # G2 表没有 stock_code; 只能补 bond 名称(通过 ts_code? 无)。跳过。
    except Exception:
        pass
    for k in m:
        m[k] = list(dict.fromkeys(m[k]))
    return m


def assign(sym, title, m):
    cand = m.get(sym, [])
    for name, code in cand:
        if name and name in title:
            return code, name
    if len(cand) == 1:
        return cand[0][1], cand[0][0]
    return None, None


def main():
    ann = load_ann()
    m = name_map()
    rows = []
    for sym, df in ann.items():
        for _, r in df.iterrows():
            t = str(r["title"])
            if "向下修正" not in t:
                continue
            kind = ("proposal" if "董事会提议" in t else
                    "trigger" if "预计触发" in t else
                    "not_revise" if "不向下修正" in t else "effective")
            code, name = assign(sym, t, m)
            rows.append({"symbol": sym, "ann_date": r["ann_date"], "title": t,
                         "kind": kind, "code": code, "bond_name": name})
    a = pd.DataFrame(rows)
    print("all rows", len(a), "| unmapped", int(a["code"].isna().sum()))
    for kind, win in (("proposal", 60), ("trigger", 20), ("not_revise", 20)):
        x = a[(a["kind"] == kind) & (a["ann_date"] >= START) & (a["ann_date"] <= END)
              & a["code"].notna()].copy()
        x["code"] = x["code"].astype(str).str.zfill(6)
        x = x.sort_values(["code", "ann_date"])
        keep, last = [], {}
        for _, r in x.iterrows():
            if r["code"] in last and (r["ann_date"] - last[r["code"]]).days < win:
                continue
            keep.append(r)
            last[r["code"]] = r["ann_date"]
        x = pd.DataFrame(keep)
        out = Path(f"data_store/meta/cb_clause_events_{kind}.parquet")
        x.to_parquet(out, index=False)
        print(f"{kind}: raw {len(a[(a['kind']==kind)&(a['ann_date']>=START)&(a['ann_date']<=END)])} "
              f"-> dedup {len(x)} | bonds {x['code'].nunique()} -> {out}")


if __name__ == "__main__":
    main()
