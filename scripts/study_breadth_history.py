"""H1 宽度序列构建(2019-2026): 逐股日线 → 每日市场宽度。

输出 logs/breadth_history.csv 一行/交易日:
  n_valid(非停牌样本) / adv_ratio / ma20_breadth / up_limit / down_limit / susp_ratio
口径与偏差声明见 logs/study_registration_breadth.md。
运行: timeout 1500 nice -n 10 ionice -c2 -n7 python3 scripts/study_breadth_history.py
"""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta  # noqa

DATA = Path("/data/quant_data_store")
OUT = Path("/root/quant/logs/breadth_history.csv")
YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
BANDS = [("60", 0.10), ("00", 0.10), ("30", 0.20), ("68", 0.20),
         ("8", 0.30), ("4", 0.30), ("92", 0.30)]


def band_of(code: str, is_st: set) -> float:
    if code in is_st:
        return 0.05
    return next((b for pfx, b in BANDS if code.startswith(pfx)), 0.10)


def main():
    info = load_meta("stock_info_full")
    is_st = ({str(c) for c, s in zip(info["code"], info["is_st"]) if s}
             if not info.empty and "is_st" in info.columns else set())

    agg = defaultdict(lambda: {"n": 0, "adv": 0, "ma_ok": 0, "above": 0,
                               "up": 0, "down": 0, "susp": 0})
    t0 = datetime.now()
    files_done = 0
    skipped = 0
    for year in YEARS:
        ydir = DATA / "daily" / str(year)
        if not ydir.exists():
            continue
        for p in sorted(ydir.glob("*.parquet")):
            files_done += 1
            if files_done % 2000 == 0:
                print(f"  {files_done} files, {datetime.now() - t0}", flush=True)
            # 闸门十二: 列集合因数据源批次而异(1440 只无 is_suspended 列)。
            # 按实际存在的列读取, 不因缺列整只跳过; 跳过数必须可见。
            try:
                d = pd.read_parquet(p)
            except Exception:
                skipped += 1
                continue
            need = {"date", "close", "pct_chg"}
            if d.empty or not need.issubset(d.columns):
                skipped += 1
                continue
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            code = str(d["code"].iloc[0]).split(".")[0] if "code" in d.columns else p.stem
            code = str(code).zfill(6)
            # is_suspended 在本库全为 NaN(无信息), 停牌以"当日无有效 bar"
            # 体现——无 bar 的日期自然不进分母。
            susp = (pd.to_numeric(d["is_suspended"], errors="coerce").fillna(0) != 0
                    if "is_suspended" in d.columns else pd.Series(False, index=d.index))
            c = pd.to_numeric(d["close"], errors="coerce")
            ma20 = c.rolling(20).mean()
            pct = pd.to_numeric(d["pct_chg"], errors="coerce")
            band = band_of(code, is_st)
            for i, dt in enumerate(d["date"].tolist()):
                if susp.iloc[i] or pd.isna(c.iloc[i]):
                    continue
                a = agg[dt]
                a["n"] += 1
                pv = pct.iloc[i]
                if pd.notna(pv):
                    if pv > 0:
                        a["adv"] += 1
                    if pv >= band * 100 - 0.6:
                        a["up"] += 1
                    elif pv <= -band * 100 + 0.6:
                        a["down"] += 1
                if pd.notna(ma20.iloc[i]):
                    a["ma_ok"] += 1
                    if c.iloc[i] > ma20.iloc[i]:
                        a["above"] += 1

    rows = []
    for dt in sorted(agg):
        a = agg[dt]
        n = a["n"]
        rows.append({
            "date": dt,
            "n_valid": n,
            "susp_ratio": round(a["susp"] / max(n + a["susp"], 1), 4),
            "adv_ratio": round(a["adv"] / n, 4) if n else None,
            "ma20_breadth": round(a["above"] / a["ma_ok"], 4) if a["ma_ok"] else None,
            "ma20_n": a["ma_ok"],
            "up_limit": a["up"],
            "down_limit": a["down"],
        })
    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"已保存 {OUT}: {len(df)} 行, {df['date'].min()} → {df['date'].max()}, "
          f"耗时 {datetime.now() - t0}")
    print(f"[覆盖] 文件 {files_done} 个, 跳过 {skipped} 个(缺列/不可读)")
    print(df.tail(3).to_string(index=False))
    # 零号闸门: 量纲/连续性/身份自检
    print("\n[自检] 每日有效样本 n_valid: "
          f"min={df['n_valid'].min()} median={int(df['n_valid'].median())} "
          f"max={df['n_valid'].max()}")
    print("[自检] adv_ratio 分布:", df["adv_ratio"].describe()[
        ["mean", "std", "min", "max"]].round(3).to_dict())
    print("[自检] ma20_breadth 分布:", df["ma20_breadth"].describe()[
        ["mean", "std", "min", "max"]].round(3).to_dict())


if __name__ == "__main__":
    main()
