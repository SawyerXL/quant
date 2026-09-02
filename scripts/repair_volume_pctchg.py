"""
全库volume单位修复 + pct_chg重算（2026-09-02，数据层P0修复）。

问题1: 东财volume=手 vs 新浪volume=股, 两源交错写同一批parquet →
      volume列100倍混用(771/800只股票2433处翻转), 量比/换手/缩量类
      指标全部静默算错。
问题2: 新浪单行取数pct_change首行NaN → pct_chg最新一周大面积NaN
      (8.1%的2026 bars), 敞口归因面板静默丢股。

检测规则:
  volume: implied = amount/close(成交额/收盘价≈真实股数); 若
          volume/implied ∈ [0.005, 0.02] 即volume≈implied/100 → 手 → ×100。
          0.5~1.5附近=股(含amount四舍五入噪声)不动; 其余=存疑跳过并计数。
  pct_chg: 跨年拼接close序列重算 pct_change×100, 首bar保持NaN(无昨收)。

用法: python scripts/repair_volume_pctchg.py          # dry-run, 只统计
      python scripts/repair_volume_pctchg.py --apply  # 写回parquet
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

DATA_STORE = Path(__file__).parent.parent / "data_store"


def load_code_files(code: str) -> dict[int, pd.DataFrame]:
    """加载一只股票全部年度文件, {year: df}。"""
    out = {}
    for f in sorted((DATA_STORE / "daily").glob("*/")):
        if not f.is_dir():
            continue
        p = f / f"{code}.parquet"
        if p.exists():
            out[int(f.name)] = pd.read_parquet(p)
    return out


def main(apply: bool):
    years = sorted(p.name for p in (DATA_STORE / "daily").iterdir()
                   if p.is_dir())
    codes = sorted({p.stem for y in years
                    for p in (DATA_STORE / "daily" / y).glob("*.parquet")})
    print(f"全库: {len(years)}个年度目录, {len(codes)}只股票", flush=True)

    n_vol_fixed = n_vol_skip = n_pct_fixed = 0
    vol_fix_files = set(); pct_fix_files = set()

    for ci, code in enumerate(codes):
        files = load_code_files(code)
        if not files:
            continue
        # 拼接成完整序列(日期升序去重; 归一化str, 新旧文件date类型混装)
        parts = [df for _, df in sorted(files.items())]
        full = pd.concat(parts)
        full["date"] = pd.to_datetime(full["date"]).dt.strftime("%Y-%m-%d")
        full = full.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)

        # ── volume 单位检测 ──
        close = pd.to_numeric(full["close"], errors="coerce")
        amt = pd.to_numeric(full["amount"], errors="coerce")
        vol = pd.to_numeric(full["volume"], errors="coerce")
        implied = amt / close.where(close > 0)
        r = vol / implied.where(implied > 0)
        mask_lot = (r >= 0.005) & (r <= 0.02)   # 手单位证据
        mask_share = (r >= 0.5) & (r <= 1.5)    # 股单位(正常)
        mask_ambiguous = (~mask_lot & ~mask_share) & implied.notna() & vol.notna()

        if mask_lot.any():
            full.loc[mask_lot, "volume"] = vol[mask_lot] * 100
            n_vol_fixed += int(mask_lot.sum())
            vol_fix_files.add(code)
        n_vol_skip += int(mask_ambiguous.sum())

        # ── pct_chg 重算 ──
        pct_new = (close.pct_change() * 100).round(4)
        pct_old = pd.to_numeric(full.get("pct_chg"), errors="coerce")
        changed = (pct_old.isna() & pct_new.notna()) | \
                  ((pct_old - pct_new).abs() > 0.011)
        if changed.any():
            full["pct_chg"] = pct_new
            n_pct_fixed += int(changed.sum())
            pct_fix_files.add(code)

        if apply and (mask_lot.any() or changed.any()):
            for y, df in files.items():
                yf = full[full["date"].astype(str).str[:4] == str(y)]
                if yf.empty:
                    continue
                path = DATA_STORE / "daily" / str(y) / f"{code}.parquet"
                keep_cols = [c for c in df.columns if c in yf.columns]
                yf = yf[keep_cols]
                yf.to_parquet(path, index=False)

        if (ci + 1) % 1000 == 0:
            print(f"  ...{ci+1}/{len(codes)} 处理中", flush=True)

    print(f"\n=== {'APPLY' if apply else 'DRY-RUN'} 汇总 ===", flush=True)
    print(f"volume: 修复{n_vol_fixed}行 (涉及{len(vol_fix_files)}只) / "
          f"存疑跳过{n_vol_skip}行", flush=True)
    print(f"pct_chg: 修复{n_pct_fixed}行 (涉及{len(pct_fix_files)}只)", flush=True)
    if not apply:
        print("未写回。确认数字后用 --apply 落盘。", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)
