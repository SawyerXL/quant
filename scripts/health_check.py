"""
系统健康检查脚本。
检查数据管道、信号文件、磁盘空间是否正常，异常时推送企业微信告警。

建议 cron 每天 17:30 运行（数据更新完成后）：
  30 17 * * 1-5 cd /root/quant && /root/quant/.venv/bin/python scripts/health_check.py >> logs/health.log 2>&1
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger
from data.storage import load_meta
from monitoring.alerts import send_alert

logger.add("logs/health_{time:YYYY-MM-DD}.log", rotation="1 week", retention="30 days")

DATA_STORE  = Path("data_store")
SIGNAL_A    = DATA_STORE / "meta" / "signal_a_latest.json"
SIGNAL_B    = DATA_STORE / "meta" / "signal_b_latest.json"
DISK_WARN_GB = 5.0      # 磁盘剩余低于 5GB 告警


# ── 检查项 ────────────────────────────────────────────────────────────

def check_daily_data_freshness() -> tuple[bool, str]:
    """最近5个交易日是否都有日线更新。"""
    cal = load_meta("trade_calendar")
    if cal.empty:
        return False, "交易日历缺失"

    today_str = date.today().strftime("%Y-%m-%d")
    past_dates = [d for d in cal["trade_date"].tolist() if d <= today_str]
    recent = sorted(past_dates)[-5:]
    year_dir = DATA_STORE / "daily" / str(date.today().year)
    if not year_dir.exists():
        return False, f"日线目录不存在: {year_dir}"

    # 抽查固定的流动性好、不易停牌的蓝筹股
    PROBE_CODES = ["600036", "600519", "601318", "000858", "000651"]   # 招商银行/茅台/平安/五粮液/格力
    sample_codes = PROBE_CODES

    # 只查昨天和前天（不查当天，因为17:00前数据尚未更新）
    check_dates = [d for d in recent if d < today_str][-2:]

    missing = []
    for trade_dt in check_dates:
        dt_ts = pd.Timestamp(trade_dt)
        for code in sample_codes:
            parquet = year_dir / f"{code}.parquet"
            if not parquet.exists():
                missing.append(f"{code}@{trade_dt}")
                continue
            df = pd.read_parquet(parquet, columns=["date"])
            if dt_ts not in pd.to_datetime(df["date"]).values:
                missing.append(f"{code}@{trade_dt}")

    # 每天允许最多1只缺失（股票停牌/API偶发问题），超过才告警
    max_allowed = len(check_dates) * 1
    if len(missing) > max_allowed:
        return False, f"日线数据缺失 {len(missing)} 处: {missing[:3]}"
    return True, f"日线正常（抽查 {len(sample_codes)} 只 × {len(check_dates)} 天，{len(missing)} 处小缺失）"


def check_signal_a_freshness() -> tuple[bool, str]:
    """Track A 信号文件是否存在且在近期更新过。"""
    if not SIGNAL_A.exists():
        return False, "signal_a_latest.json 不存在"

    data = json.loads(SIGNAL_A.read_text(encoding="utf-8"))
    sig_date = data.get("signal_date", "")
    if not sig_date:
        return False, "信号文件格式异常"

    sig_dt   = date.fromisoformat(sig_date)
    days_old = (date.today() - sig_dt).days
    holdings = len(data.get("holdings", []))

    if days_old > 20:     # 超过20天未更新（约一个调仓周期）
        return False, f"Track A 信号 {days_old} 天未更新（最后: {sig_date}）"
    return True, f"Track A 信号正常 ({sig_date}, {holdings} 只持仓, {days_old} 天前)"


def check_signal_b_freshness() -> tuple[bool, str]:
    """
    Track B 信号文件检查。
    路线B（人机协作）下：信号由人工判断驱动，不强制要求每周更新。
    仅检查文件存在性；若存在且超过45天（约6个调仓周期）才报警。
    """
    if not SIGNAL_B.exists():
        return True, "Track B 路线B模式：信号由人工判断驱动，量化暂不生成"

    data = json.loads(SIGNAL_B.read_text(encoding="utf-8"))
    sig_date = data.get("signal_date", "")
    if not sig_date:
        return True, "Track B 信号文件格式异常但不影响运行"

    sig_dt   = date.fromisoformat(sig_date)
    days_old = (date.today() - sig_dt).days

    if days_old > 45:     # 路线B下放宽到45天（6个调仓周期）
        return False, f"Track B 信号 {days_old} 天未更新，请确认是否需要操作"
    return True, f"Track B 路线B（最后信号: {sig_date}，{days_old} 天前）"


def check_stock_meta_freshness() -> tuple[bool, str]:
    """stock_info_full 是否存在且行业字段完整。"""
    info = load_meta("stock_info_full")
    if info.empty:
        return False, "stock_info_full 不存在，请运行 init_stock_meta.py"

    if "industry_l1" not in info.columns:
        return False, "stock_info_full 缺少 industry_l1 字段"

    no_industry = (info["industry_l1"].isna() | (info["industry_l1"] == "其他")).sum()
    total = len(info)
    if no_industry / total > 0.30:
        return False, f"行业数据覆盖率过低 ({total - no_industry}/{total})"

    st_count = info["is_st"].sum() if "is_st" in info.columns else 0
    return True, f"stock_info_full 正常 ({total} 只, ST={st_count}, 无行业={no_industry})"


def check_disk_space() -> tuple[bool, str]:
    """检查 data_store 所在磁盘剩余空间。"""
    import shutil
    usage = shutil.disk_usage(DATA_STORE)
    free_gb = usage.free / 1024 ** 3
    total_gb = usage.total / 1024 ** 3
    used_pct = (usage.used / usage.total) * 100

    if free_gb < DISK_WARN_GB:
        return False, f"磁盘剩余仅 {free_gb:.1f} GB（已用 {used_pct:.0f}%），请及时清理"
    return True, f"磁盘正常（剩余 {free_gb:.1f} / {total_gb:.0f} GB，已用 {used_pct:.0f}%）"


def check_csi_index_freshness() -> tuple[bool, str]:
    """CSI 800 / CSI 1000 成分股是否有效。"""
    csi800  = load_meta("csi800")
    csi1000 = load_meta("csi1000")
    issues  = []
    if csi800.empty or len(csi800) < 700:
        issues.append(f"csi800 异常 ({len(csi800)} 只)")
    if csi1000.empty or len(csi1000) < 900:
        issues.append(f"csi1000 异常 ({len(csi1000)} 只)")
    if issues:
        return False, "；".join(issues)
    return True, f"CSI 800({len(csi800)}) + CSI 1000({len(csi1000)}) 正常"


# ── 主流程 ────────────────────────────────────────────────────────────

def run():
    logger.info("=" * 50)
    logger.info(f"健康检查开始: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 50)

    checks = [
        ("日线数据新鲜度",   check_daily_data_freshness),
        ("Track A 信号",    check_signal_a_freshness),
        ("Track B 信号",    check_signal_b_freshness),
        ("股票元数据",       check_stock_meta_freshness),
        ("CSI 指数成分",    check_csi_index_freshness),
        ("磁盘空间",        check_disk_space),
    ]

    failures = []
    for name, fn in checks:
        try:
            ok, msg = fn()
            status = "✅" if ok else "❌"
            logger.info(f"  {status} {name}: {msg}")
            if not ok:
                failures.append(f"{name}: {msg}")
        except Exception as e:
            logger.error(f"  ❌ {name}: 检查出错 — {e}")
            failures.append(f"{name}: 检查出错 — {e}")

    logger.info("=" * 50)
    if failures:
        summary = f"【健康检查告警】{date.today()}\n共 {len(failures)} 项异常：\n"
        summary += "\n".join(f"❌ {f}" for f in failures)
        logger.warning(summary)
        send_alert(summary, level="warning")
    else:
        msg = f"【健康检查】{date.today()} 全部正常 ✅"
        logger.info(msg)
        send_alert(msg, level="info")


if __name__ == "__main__":
    run()
