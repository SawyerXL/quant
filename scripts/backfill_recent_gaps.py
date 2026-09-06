"""
补最近交易日空洞 — 一次性大面积补洞工具（2026-09-06 建）
用法: python scripts/backfill_recent_gaps.py

与 daily_data_update._fill_recent_gaps 的区别: 单次扫描 + 全量修复,
不受 GAP_MAX_REPAIR=400/run 限制——适合故障窗口(如 9/2-9/4 数据更新
bug 造成的 5547 只全市场缺口)的恢复。逐只拉取, 尊重 gap_skip 停牌表。
"""
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger
from config.settings import LOG_DIR
logger.add(LOG_DIR / "backfill_recent.log", rotation="7 days")

from data.source import get_source
from data.storage import load_daily, save_daily, load_meta
from scripts.daily_data_update import GAP_LOOKBACK, GAP_SKIP_PATH, GAP_SKIP_MAX, INDEX_CODES


def main():
    src = get_source()
    calendar = src.get_trade_calendar()
    today = date.today().strftime("%Y-%m-%d")
    recent = [d for d in calendar if d <= today][-GAP_LOOKBACK:]
    if len(recent) < 2:
        logger.error("交易日历不足"); return
    lo, want = recent[0], set(recent)

    skip = {}
    if GAP_SKIP_PATH.exists():
        try:
            skip = json.loads(GAP_SKIP_PATH.read_text())
        except Exception:
            skip = {}

    info = load_meta("stock_info_full")
    if info.empty:
        info = load_meta("stock_info")
    codes = [c for c in info["code"].tolist() if c not in INDEX_CODES]

    holes = []
    for code in codes:
        try:
            d = load_daily(code, lo, today)
            have = set(pd.to_datetime(d["date"]).astype(str).str[:10]) if not d.empty else set()
            miss = sorted(x for x in want - have if skip.get(f"{code}:{x}", 0) < GAP_SKIP_MAX)
            if miss:
                holes.append((code, miss))
        except Exception:
            continue
    logger.info(f"空洞扫描: {len(holes)}只有缺口 / 共{len(codes)}只, 窗口 {lo}~{recent[-1]}")
    if not holes:
        return

    fixed = failed = 0
    for i, (code, miss) in enumerate(holes):
        try:
            df = src.get_daily(code, miss[0], miss[-1])
            if df is not None and not df.empty:
                save_daily(code, df)
            after = load_daily(code, lo, today)
            have = set(pd.to_datetime(after["date"]).astype(str).str[:10]) if not after.empty else set()
            still = [x for x in miss if x not in have]
        except Exception as e:
            logger.debug(f"{code} 补洞失败: {e}")
            still = miss
        if still:
            failed += 1
            for x in still:
                skip[f"{code}:{x}"] = skip.get(f"{code}:{x}", 0) + 1
        else:
            fixed += 1
        if (i + 1) % 200 == 0:
            logger.info(f"进度: {i+1}/{len(holes)}  修复{fixed} 失败{failed}")
        time.sleep(0.05)  # 温和限速, 防新浪源触发封禁

    GAP_SKIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    GAP_SKIP_PATH.write_text(json.dumps(skip))
    logger.info(f"补洞完成: 修复{fixed}只, 失败{failed}只, 跳过表{len(skip)}条")


if __name__ == "__main__":
    main()
