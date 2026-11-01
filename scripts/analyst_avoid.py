"""分析师“调低”回避名单（可选股票腿 overlay，2026-11-01）。

默认关闭：`ANALYST_AVOID_DAYS` 未设置/0 时返回空集，生产行为不变。
启用时读 `data_store/meta/analyst_downgrades.parquet`，取调仓日前
N 个交易日内有调低的股票。只读；不做下单。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data_store" / "meta" / "analyst_downgrades.parquet"


def env_days() -> int:
    raw = os.getenv("ANALYST_AVOID_DAYS", "0").strip()
    try:
        n = int(raw)
    except ValueError:
        logger.warning(f"ANALYST_AVOID_DAYS 非法({raw}), 按 0 处理")
        return 0
    return max(0, n)


def load_events(path: Path = DEFAULT_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["date", "code"])
    d = pd.read_parquet(path)
    d["code"] = d["code"].astype(str).str.zfill(6)
    d["date"] = d["date"].astype(str).str[:10]
    return d.drop_duplicates(["date", "code"])


def blacklist(asof: str, calendar: list[str], days: int,
              path: Path = DEFAULT_PATH) -> set[str]:
    """返回 (asof-days 交易日, asof) 内调低的代码；asof 当日不算。"""
    if days <= 0:
        return set()
    ev = load_events(path)
    if ev.empty:
        return set()
    idx = {d: i for i, d in enumerate(calendar)}
    i = idx.get(asof)
    if i is None:
        # 非交易日：用最近的前一交易日边界，保守不引入未来
        prior = [d for d in calendar if d <= asof]
        if not prior:
            return set()
        i = idx[prior[-1]]
    lo = calendar[max(0, i - days)]
    x = ev[(ev["date"] >= lo) & (ev["date"] < asof)]
    return set(x["code"])


def get_blacklist(asof: str, calendar: list[str],
                  path: Path = DEFAULT_PATH) -> set[str]:
    days = env_days()
    if days <= 0:
        return set()
    return blacklist(asof, calendar, days, path)
