"""D 分层复权重建 (2026-10-21, C 之后按顺序).

宇宙: `logs/qfq_d_universe_20261021.csv` 的 **rebal60 并集（882只，
策略实际候选池）** 中 registry 非 qfq 的 **453 只**（排除北交所与
601059/601198 尾部缺口）。日度 TOP300 并集=4782≈全市场，不做（B 暂缓）。
方法沿用 A: 源 qfq 重拉 + 前沿锚（±0.5%）+ 备份 + 按年覆写；新增
"源端 max 之后已有 bar 保留"（防覆写丢当日）。as_of=2026-10-21。
用法: python scripts/rebuild_d_universe_qfq.py
"""
import argparse
import signal
import sys
import shutil
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from loguru import logger
from data.source import get_source
from data.storage import load_daily

END = "2026-10-21"   # 工件 as_of 冻结(闸门十三)
ANCHOR_LO = "2026-10-01"  # 最新bar锚窗口
BAK = Path("data_store/backup_qfq_rebuild_20261021")


def _timeout_handler(signum, frame):
    raise TimeoutError("source fetch timeout 150s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", default=None,
                    help="断点续跑: 每行一个代码; 只处理这些码")
    ap.add_argument("--json-out", default="logs/rebuild_d_universe_20261021.json")
    args = ap.parse_args()
    uni = pd.read_csv("logs/qfq_d_universe_20261021.csv", dtype={"code": str})
    uni["code"] = uni["code"].str.zfill(6)
    cand = set(uni.loc[uni["rebal60"], "code"])
    reg = pd.read_parquet("data_store/meta/adjust_basis_registry.parquet")
    qfq = set(reg.loc[reg["basis"] == "qfq", "code"].astype(str))
    if args.codes_file:
        want = {ln.strip().zfill(6) for ln in
                Path(args.codes_file).read_text().splitlines() if ln.strip()}
        codes = sorted(c for c in want
                       if c in cand
                       and not c.startswith(("4", "8", "92"))
                       and c not in {"601059", "601198"})
    else:
        codes = sorted(c for c in cand
                       if c not in qfq
                       and not c.startswith(("4", "8", "92"))
                       and c not in {"601059", "601198"})
    print(f"D 宇宙: rebal60 {len(cand)} / 非qfq {len(codes)} "
          f"(排除北交所与 601059/601198)", flush=True)

    src = get_source()
    BAK.mkdir(parents=True, exist_ok=True)
    fixed, skipped, failed, tail_kept = [], [], [], 0
    for i, code in enumerate(codes):
        try:
            # 2026-10-21 首跑在 600572 附近无限挂起被 2h timeout 杀; 单券
            # 150s 硬超时→FallbackDataSource 捕获后落 MCP 备源(其自带 30s)
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(150)
            try:
                df = src.get_daily(code, "1990-01-01", END)
            finally:
                signal.alarm(0)
            if df is None or df.empty:
                failed.append((code, "拉取空"))
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            if df.empty:
                failed.append((code, "清洗后空"))
                continue
            # 前沿锚: 只比锚窗口内最后共同日(前复权在数据前沿恒等于原值,
            # 与库内口径形态无关——窗内更早日期会被新除权重缩放, MAX会误伤
            # 分红密集票; 2026-10-11 实测 13 只高分红央企被 MAX 误跳过,
            # 前沿偏离全部精确 0.00000 后覆写 11 只)
            stored = load_daily(code, ANCHOR_LO, END)
            ok_anchor = False
            if stored is not None and not stored.empty:
                s = stored.set_index(pd.to_datetime(stored["date"]))["close"]
                f = df.set_index("date")["close"]
                common = s.index.intersection(f.index)
                if len(common):
                    last = common[-1]
                    dev = abs(float(f.loc[last]) / float(s.loc[last]) - 1)
                    if dev <= 0.005:
                        ok_anchor = True
                    else:
                        logger.warning(f"{code} 前沿锚不过(偏离{dev:.4f}), 跳过")
                        skipped.append((code, f"前沿偏离{dev:.4f}"))
                        continue
                else:
                    logger.warning(f"{code} 锚窗口无共同日, 跳过")
                    skipped.append((code, "锚窗口无共同日"))
                    continue
            else:
                logger.warning(f"{code} 库内锚窗口无数据, 跳过")
                skipped.append((code, "库内锚窗口无数据"))
                continue
            src_max = df["date"].max()
            # 备份 + 按年直写
            for yr in range(1990, 2027):
                p = Path(f"data_store/daily/{yr}/{code}.parquet")
                if p.exists():
                    (BAK / str(yr)).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, BAK / str(yr) / f"{code}.parquet")
            for yr, grp in df.groupby(df["date"].dt.year):
                dyr = Path(f"data_store/daily/{yr}")
                dyr.mkdir(parents=True, exist_ok=True)
                fp = dyr / f"{code}.parquet"   # 2026-10-21 修: 原读目录
                # 源端 max 之后已有 bar(日更写入)保留, 防覆写丢当日
                if fp.exists():
                    try:
                        old = pd.read_parquet(fp)
                        old["date"] = pd.to_datetime(old["date"])
                        tail = old[old["date"] > src_max]
                        if len(tail):
                            grp = pd.concat([grp, tail], ignore_index=True
                                            ).sort_values("date")
                            tail_kept += 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"{code} {yr} 尾部保留读取失败: {e}")
                grp.to_parquet(fp, index=False)
            fixed.append(code)
            print(f"  {code}: 全史qfq覆写 {len(df)}行", flush=True)
        except Exception as e:
            logger.warning(f"{code} 失败: {e}")
            failed.append((code, str(e)[:80]))
        if (i + 1) % 100 == 0:
            print(f"进度 {i+1}/{len(codes)}, 覆写 {len(fixed)} 跳过 {len(skipped)} "
                  f"失败 {len(failed)}", flush=True)
    print(f"\n覆写 {len(fixed)} 只 / 锚跳过 {len(skipped)} 只 / 失败 {len(failed)} 只", flush=True)
    if skipped:
        print(f"锚跳过: {skipped[:20]}{'...' if len(skipped) > 20 else ''}", flush=True)
    if failed:
        print(f"失败: {failed[:20]}{'...' if len(failed) > 20 else ''}", flush=True)
    import json
    Path(args.json_out).write_text(
        json.dumps({"as_of": END, "anchor_lo": ANCHOR_LO,
                    "n_universe_rebal60": len(cand), "n_codes": len(codes),
                    "n_fixed": len(fixed), "n_skipped": len(skipped),
                    "n_failed": len(failed), "n_tail_kept": tail_kept,
                    "fixed": fixed, "skipped": skipped, "failed": failed},
                   ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
