"""
MA10-4d触发追踪 — 每日盘后自动检查+更新
用法: python scripts/check_ma10_triggers.py [--send]
职责:
  1. 扫描 dual_report 输出，找出新增 MA10-4d 触发
  2. 新触发 → 追加到 config/ma10_4d_trigger_log.csv
  3. 已有触发 → 检查 T+7/T+30 是否到期，回填价格
  4. T+30 到期 → 判定 verdict (有效/无效)
  5. 状态摘要 → 发邮件
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from loguru import logger

from config.settings import ROOT, LOG_DIR
logger.add(LOG_DIR / "ma10_trigger.log", rotation="7 days")

TRACK_FILE = ROOT / "config/ma10_4d_trigger_log.csv"
HOLDINGS_FILE = ROOT / "config/my_holdings.csv"
TRACK_COLS = [
    "trigger_date", "code", "name", "trigger_price", "cost_price",
    "pl_pct_at_trigger", "ma10_at_trigger", "days_below_ma10",
    "action_taken", "actual_sell_date", "actual_sell_price",
    "price_7d", "price_30d", "chg_7d_pct", "chg_30d_pct",
    "verdict", "notes"
]


def load_log():
    if TRACK_FILE.exists():
        df = pd.read_csv(TRACK_FILE, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        return df
    return pd.DataFrame(columns=TRACK_COLS)


def save_log(df):
    TRACK_FILE.parent.mkdir(exist_ok=True)
    df.to_csv(TRACK_FILE, index=False)
    logger.info(f"追踪表已保存: {len(df)}条")


def get_price_on_date(code, target_date):
    """获取指定日期的收盘价。"""
    from data.storage import load_daily
    try:
        df = load_daily(code, target_date, target_date)
        if not df.empty and "close" in df.columns:
            return float(df["close"].iloc[-1])
    except:
        pass
    return None


def find_new_triggers():
    """
    扫描 current holdings + MA10 状态, 找出昨晚收盘新触发的 MA10-4d 信号。
    复用 morning_dual_report 的 MA10 计算逻辑。
    """
    today_str = date.today().strftime("%Y-%m-%d")
    from data.storage import load_daily

    # 加载持仓
    df_h = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df_h["code"] = df_h["code"].str.zfill(6)
    held = df_h[(df_h["monitor"] == True) & (df_h["shares"] > 0)]

    triggers = []
    for _, r in held.iterrows():
        code = r["code"]
        name = r["name"]
        cost = float(r["cost_price"])
        try:
            d = load_daily(code,
                           (pd.Timestamp(today_str) - pd.Timedelta(days=40)).strftime("%Y-%m-%d"),
                           today_str)
            if d.empty or "close" not in d.columns:
                continue
            d = d.sort_values("date")
            closes = d["close"].dropna()
            if len(closes) < 11:
                continue

            # MA10 = 最近10日(不含今天)的收盘价均值
            recent = closes.tail(11)
            ma10 = float(recent.iloc[:10].mean())

            # 破MA10天数: 从最近开始往前数连续 < MA10 的天数
            days_below = 0
            for i in range(len(recent) - 1, -1, -1):
                if float(recent.iloc[i]) < ma10:
                    days_below += 1
                else:
                    break

            price = float(closes.iloc[-1])
            pl_pct = (price / cost - 1) * 100

            # 触发条件: 首次达到4天(不是每天重复)。之后天数递增会跳过。
            if days_below == 4:
                triggers.append({
                    "trigger_date": today_str,
                    "code": code,
                    "name": name,
                    "trigger_price": round(price, 2),
                    "cost_price": round(cost, 2),
                    "pl_pct_at_trigger": round(pl_pct, 1),
                    "ma10_at_trigger": round(ma10, 2),
                    "days_below_ma10": days_below,
                    "action_taken": "否",
                    "actual_sell_date": "",
                    "actual_sell_price": "",
                    "price_7d": "",
                    "price_30d": "",
                    "chg_7d_pct": "",
                    "chg_30d_pct": "",
                    "verdict": "待判定",
                    "notes": "",
                })
        except Exception as e:
            logger.debug(f"{code} 检查跳过: {e}")

    return triggers


def find_trade_calendar():
    """加载交易日历。"""
    from data.storage import load_meta
    cal = load_meta("trade_calendar")
    if cal.empty:
        return []
    return sorted(cal["trade_date"].tolist())


def get_nth_trading_day_after(start_date, n, calendar):
    """获取start_date之后第n个交易日的日期。"""
    start_str = start_date if isinstance(start_date, str) else start_date.strftime("%Y-%m-%d")
    idx = None
    for i, d in enumerate(calendar):
        if str(d)[:10] == start_str:
            idx = i
            break
    if idx is None:
        # 近似: 日历日
        target = pd.Timestamp(start_str) + pd.Timedelta(days=n + int(n / 5) * 2)
        return target.strftime("%Y-%m-%d")
    target_idx = idx + n
    if target_idx < len(calendar):
        return str(calendar[target_idx])[:10]
    return None


def backfill_prices(df):
    """回填 T+7 和 T+30 价格。"""
    today_str = date.today().strftime("%Y-%m-%d")
    calendar = find_trade_calendar()
    updated = 0

    for idx, row in df.iterrows():
        if row["verdict"] != "待判定":
            continue

        trigger_d = str(row["trigger_date"])[:10]

        # T+7 检查
        if pd.isna(row.get("price_7d")) or str(row["price_7d"]).strip() in ("", "nan"):
            t7 = get_nth_trading_day_after(trigger_d, 7, calendar)
            if t7 and t7 <= today_str:
                p7 = get_price_on_date(row["code"], t7)
                if p7:
                    df.at[idx, "price_7d"] = round(p7, 2)
                    df.at[idx, "chg_7d_pct"] = round((p7 / row["trigger_price"] - 1) * 100, 1)
                    logger.info(f"  T+7 回填: {row['code']} {row['name']} "
                                f"触发¥{row['trigger_price']} → T+7({t7}) ¥{p7} "
                                f"({df.at[idx, 'chg_7d_pct']:+.1f}%)")
                    updated += 1

        # T+30 检查
        if pd.isna(row.get("price_30d")) or str(row["price_30d"]).strip() in ("", "nan"):
            t30 = get_nth_trading_day_after(trigger_d, 30, calendar)
            if t30 and t30 <= today_str:
                p30 = get_price_on_date(row["code"], t30)
                if p30:
                    df.at[idx, "price_30d"] = round(p30, 2)
                    chg30 = round((p30 / row["trigger_price"] - 1) * 100, 1)
                    df.at[idx, "chg_30d_pct"] = chg30
                    # 判定
                    if chg30 < -3:
                        df.at[idx, "verdict"] = "有效(持续跌)"
                    elif chg30 > 3:
                        df.at[idx, "verdict"] = "无效(反弹了)"
                    else:
                        df.at[idx, "verdict"] = "中性(±3%)"
                    logger.info(f"  T+30 判定: {row['code']} {row['name']} → {df.at[idx, 'verdict']} "
                                f"({chg30:+.1f}%)")
                    updated += 1

    if updated:
        logger.info(f"回填完成: {updated}条更新")
    return df


def run(send=False):
    today_str = date.today().strftime("%Y-%m-%d")
    logger.info(f"MA10-4d触发追踪 {today_str}")

    # ── 1. 加载现有记录 ──
    df = load_log()
    n_existing = len(df)
    existing_codes = set()
    if not df.empty:
        existing_codes = set(zip(df["code"], df["trigger_date"]))

    # ── 2. 扫描新触发 ──
    new_triggers = find_new_triggers()
    new_added = 0
    for t in new_triggers:
        key = (t["code"], t["trigger_date"])
        # 同时检查：同一股票5天内是否已有记录（避免MA10每天偏移导致重复）
        if not df.empty:
            recent_same = df[(df["code"] == t["code"]) &
                             (pd.to_datetime(df["trigger_date"]) >=
                              pd.Timestamp(t["trigger_date"]) - pd.Timedelta(days=5))]
            if len(recent_same) > 0:
                continue  # 跳过，已有近期记录
        if key not in existing_codes:
            df = pd.concat([df, pd.DataFrame([t])], ignore_index=True)
            existing_codes.add(key)
            new_added += 1
            logger.info(f"  新触发: {t['code']} {t['name']} MA10下{t['days_below_ma10']}d "
                        f"¥{t['trigger_price']} ({t['pl_pct_at_trigger']:+.1f}%)")

    # ── 3. 回填T+7/T+30 ──
    df = backfill_prices(df)

    # ── 4. 保存 ──
    save_log(df)

    # ── 5. 统计 ──
    total = len(df)
    pending = len(df[df["verdict"] == "待判定"])
    effective = len(df[df["verdict"].str.contains("有效", na=False)])
    ineffective = len(df[df["verdict"].str.contains("无效", na=False)])
    neutral = len(df[df["verdict"].str.contains("中性", na=False)])
    judged = effective + ineffective + neutral
    no_action = len(df[(df["action_taken"] == "否") & (df["verdict"] == "待判定")])

    lines = []
    lines.append(f"📊 MA10-4d触发追踪 {today_str}")
    lines.append("")
    lines.append(f"  总触发: {total}条 | 新增: {new_added}条 | 待回填: {pending}条")
    lines.append(f"  已判定: {judged}条 | 有效: {effective} | 无效: {ineffective} | 中性: {neutral}")
    if judged > 0:
        rate = effective / judged * 100
        lines.append(f"  有效率: {rate:.0f}% ({effective}/{judged}) 目标>60%")
    lines.append("")

    if no_action > 0:
        lines.append(f"  ⚠️ {no_action}条触发未执行卖出 → 请检查是否要操作:")
        for _, r in df[(df["action_taken"] == "否") & (df["verdict"] == "待判定")].iterrows():
            lines.append(f"    🔴 {r['code']} {r['name']}: {r['trigger_date']}触发 "
                         f"¥{r['trigger_price']} ({r['pl_pct_at_trigger']:+.1f}%) "
                         f"MA10下{r['days_below_ma10']}d")

    body = "\n".join(lines)
    print(body)

    if send:
        from monitoring.alerts import _send_email
        rate_str = f" 有效{effective}/{judged}" if judged > 0 else "无判定"
        _send_email(f"[量化追踪] MA10-4d触发 {today_str} {rate_str}", body)
        logger.info("追踪邮件已发送")

    return df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    run(send=args.send)
