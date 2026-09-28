"""CB 日线目录逐券 diff (2026-09-28 建, 10-09 受控实验用)。

对比备份目录 vs 当前目录, 逐券输出: 行数变化 / 同日 close 差异行数 /
最大价格差% / 起止日变化。直接回答"A(源修订历史)"至今未关闭的问题:
重拉后有多少只的历史 bar 变了、变了多少。
用法: python scripts/cb_daily_diff.py --old <备份目录> --new <当前目录>
"""
import argparse
from pathlib import Path

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    args = ap.parse_args()

    old_d = Path(args.old)
    new_d = Path(args.new)
    old_files = {f.stem: f for f in old_d.glob("*.parquet")}
    new_files = {f.stem: f for f in new_d.glob("*.parquet")}

    rows = []
    for code in sorted(set(old_files) | set(new_files)):
        of, nf = old_files.get(code), new_files.get(code)
        if of is None or nf is None:
            rows.append(dict(code=code, old_n=0 if of is None else None,
                             new_n=0 if nf is None else None,
                             changed=0, max_pct=0.0, first="", last="",
                             note="仅单侧存在"))
            continue
        od = pd.read_parquet(of)
        nd = pd.read_parquet(nf)
        od["date"] = pd.to_datetime(od["date"])
        nd["date"] = pd.to_datetime(nd["date"])
        od = od.set_index("date").sort_index()
        nd = nd.set_index("date").sort_index()
        common = od.index.intersection(nd.index)
        changed = 0
        max_pct = 0.0
        if len(common):
            oc = od.loc[common, "close"].astype(float)
            nc = nd.loc[common, "close"].astype(float)
            d = (nc - oc).abs()
            changed = int((d > 1e-9).sum())
            pct = (d / oc.replace(0, float("nan"))).dropna()
            max_pct = float(pct.max()) if len(pct) else 0.0
        rows.append(dict(code=code, old_n=len(od), new_n=len(nd),
                         changed=changed, max_pct=round(max_pct, 6),
                         first=str(nd.index[0].date()),
                         last=str(nd.index[-1].date()),
                         note=""))

    df = pd.DataFrame(rows)
    out = "logs/cb_daily_diff_result.csv"
    df.to_csv(out, index=False)
    n_hist_changed = int((df["changed"] > 0).sum())
    n_rows_changed = int((df["new_n"] != df["old_n"]).sum())
    print(f"总券数: {len(df)} | 历史 bar 有差异的券: {n_hist_changed} | "
          f"行数变化(尾部延长等): {n_rows_changed}")
    if n_hist_changed:
        sub = df[df["changed"] > 0].sort_values("changed", ascending=False)
        print("历史 bar 差异最多的 10 只:")
        print(sub.head(10).to_string(index=False))
        print(f"  同日差异行数合计: {df['changed'].sum()} | 最大单行价差: {df['max_pct'].max()*100:.2f}%")
    print(f"已保存 {out}")


if __name__ == "__main__":
    main()
