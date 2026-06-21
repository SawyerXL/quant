"""
一次性回补 6/16-6/18 全市场日线（东财封IP期间直连新浪，绕开失败重试）。
用法: python scripts/backfill_0616_0618.py
"""
import sys, time, glob
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from data.source.akshare_source import AkshareSource
from data.storage import save_daily

START, END = "2026-06-16", "2026-06-18"
WORKERS = 6
src = AkshareSource()

# 全市场 = 本地已有的 2026 日线文件名（含已建仓/全A），排除指数 000906
codes = sorted({Path(f).stem for f in glob.glob("data_store/daily/2026/*.parquet")} - {"000906"})
logger.info(f"回补 {START}~{END}  共 {len(codes)} 只  {WORKERS}线程直连新浪")

ok = fail = empty = 0; bad = []
def work(code):
    try:
        df = src._daily_sina(code, START, END)   # 东财封IP期间直接走新浪
        if df.empty:
            return code, "empty"
        save_daily(code, df)
        return code, "ok"
    except Exception as e:
        return code, f"err:{e}"

t0 = time.time()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(work, c): c for c in codes}
    for i, fu in enumerate(as_completed(futs), 1):
        code, st = fu.result()
        if st == "ok": ok += 1
        elif st == "empty": empty += 1
        else: fail += 1; bad.append(code)
        if i % 500 == 0:
            logger.info(f"进度 {i}/{len(codes)}  成功{ok} 空{empty} 失败{fail}  用时{time.time()-t0:.0f}s")

logger.info(f"完成: 成功{ok} 空{empty} 失败{fail} / 共{len(codes)}  用时{time.time()-t0:.0f}s")
if bad:
    Path("logs/backfill_failed_codes.txt").write_text("\n".join(bad))
    logger.warning(f"失败{len(bad)}只已写 logs/backfill_failed_codes.txt（可重跑）")
