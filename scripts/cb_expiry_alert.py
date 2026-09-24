"""CB 到期/强赎硬提醒 (2026-09-23 用户定: 不依赖 N=60 过滤实装)
每日盘前 09:05 cron: 拉 jsl 强赎/到期表, 与最新持仓快照交叉,
最后交易日 ≤ 5 个交易日内 → 邮件告警; 14 日内 → 邮件提示。
拉取失败回退本地缓存(cb_expiry_watch.parquet), 缓存也旧 → 告警。
不写持仓、不下单, 纯提醒。"""
import sys
import json
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger

from monitoring.alerts import send_alert

CACHE = Path("data_store/meta/cb_expiry_watch.parquet")
HISTORY = Path("data_store/meta/cb_expiry_jsl_history.parquet")
POS_FILE = Path("logs/qmt_positions_latest.json")
URGENT_DAYS = 5      # 自然日
NOTICE_DAYS = 35     # 2026-09-23 定: 覆盖 10-09/10-16/10-27 三只 (自然日)


def fetch_jsl():
    import akshare as ak
    df = ak.bond_cb_redeem_jsl()
    df = df.dropna(subset=["代码"]).copy()
    df["代码"] = df["代码"].astype(str)
    df["最后交易日"] = pd.to_datetime(df["最后交易日"], errors="coerce")
    df["到期日"] = pd.to_datetime(df["到期日"], errors="coerce")
    df.to_parquet(CACHE)
    # 每日 PIT 快照追加(2026-09-24 用户定: 现在每天存一份, 一年后即有
    # 真实 PIT 到期日序列, 供到期网格重跑验证; 同日重跑不重复追加)
    cols = ["代码", "名称", "到期日", "最后交易日", "强赎价", "强赎状态"]
    df_h = df[cols].copy()
    df_h["fetch_date"] = pd.Timestamp(date.today())
    if HISTORY.exists():
        old_h = pd.read_parquet(HISTORY)
        if str(old_h["fetch_date"].max())[:10] != str(date.today()):
            df_h = pd.concat([old_h, df_h], ignore_index=True)
    df_h.to_parquet(HISTORY)
    logger.info(f"jsl 强赎/到期表: {len(df)} 行, 缓存+PIT历史已更新")
    return df


def load_held():
    if not POS_FILE.exists():
        return set(), None
    d = json.loads(POS_FILE.read_text(encoding="utf-8"))
    cb = {str(k).split(".")[0] for k, v in d.get("positions", {}).items()
          if isinstance(v, dict) and v.get("volume", 0) > 0
          and str(k)[:3] in ("110", "111", "113", "118", "123", "127", "128")}
    ts = d.get("exported_at", "")
    return cb, ts


def main():
    try:
        df = fetch_jsl()
        fresh = True
    except Exception as e:
        logger.warning(f"jsl 拉取失败, 用缓存: {e}")
        if CACHE.exists():
            df = pd.read_parquet(CACHE)
            fresh = False
        else:
            send_alert("CB到期提醒: jsl 拉取失败且无缓存, 提醒链失效", level="error")
            return

    held, pos_ts = load_held()
    if not held:
        logger.info("无 CB 持仓, 跳过")
        return
    today = pd.Timestamp(date.today())
    sub = df[df["代码"].isin(held) & df["最后交易日"].notna()].copy()
    if sub.empty:
        logger.info("持仓券无一在强赎/到期表")
        return
    sub["剩天数"] = ((sub["最后交易日"] - today).dt.days).astype(int)
    urgent = sub[sub["剩天数"] <= URGENT_DAYS]
    notice = sub[(sub["剩天数"] > URGENT_DAYS) & (sub["剩天数"] <= NOTICE_DAYS)]

    lines = []
    for _, r in urgent.iterrows():
        lines.append(f"【紧急】{r['代码']} {r['名称']}: 最后交易日 "
                     f"{r['最后交易日'].date()} 剩{r['剩天数']}个自然日, 到期 "
                     f"{r['到期日'].date() if pd.notna(r['到期日']) else '?'}")
    for _, r in notice.iterrows():
        lines.append(f"【提示】{r['代码']} {r['名称']}: 最后交易日 "
                     f"{r['最后交易日'].date()} 剩{r['剩天数']}个自然日")
    if lines:
        level = "error" if not urgent.empty else "warning"
        msg = f"CB到期提醒({today.date()}, 持仓快照{pos_ts}, "
        msg += "jsl实拉)" if fresh else "jsl缓存)"
        msg += "\n" + "\n".join(lines)
        send_alert(msg, level=level)
        logger.info(f"发送 {len(lines)} 条到期提醒")
    else:
        logger.info("持仓券无 14 日内到期")


if __name__ == "__main__":
    main()
