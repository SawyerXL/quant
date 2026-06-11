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

def check_signal_heartbeat() -> tuple[bool, str]:
    """信号心跳检查：确认今早信号已生成（防cron失败后无信号）"""
    sig_f = Path("data_store/meta/signal_a_latest.json")
    if not sig_f.exists():
        return False, "信号文件不存在，cron可能失败！"
    import json
    sig = json.loads(sig_f.read_text(encoding="utf-8"))
    sig_date = sig.get("signal_date", "")
    gen_at   = sig.get("generated_at", "")[:10]
    try:
        days_old = (date.today() - date.fromisoformat(sig_date)).days
    except Exception:
        days_old = 999
    if days_old > 14:
        return False, f"信号{days_old}天未更新(最后:{sig_date})，cron可能失效！"
    return True, f"信号心跳正常({sig_date}, 生成{gen_at})"


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


def check_execution_quality() -> tuple[bool, str]:
    """
    读取 fetch_and_execute.py 推回的 execution_result_{YYYYMMDD}.json，
    检查三项执行质量指标：
      1. 实际成交价 vs 信号假设价偏差（阈值：单边 >0.3% 且连续出现）
      2. 实际成交率（阈值：<90%）
      3. 全链路时延（阈值：>30min 告警）
    """
    SLIP_WARN_PCT  = 0.30   # 单边滑点告警阈值（%）
    FILL_WARN_PCT  = 90.0   # 成交率告警阈值（%）
    LATENCY_WARN   = 30.0   # 链路时延告警阈值（分钟）
    SLIP_CONSEC    = 3      # 连续N次超阈值才升级为告警

    logs_dir = Path("logs")
    # 找最近30天内的执行结果文件
    result_files = sorted(logs_dir.glob("execution_result_*.json"), reverse=True)[:10]
    if not result_files:
        return True, "暂无执行结果文件（首次调仓后自动生成）"

    latest = json.loads(result_files[0].read_text(encoding="utf-8"))
    sig_date = latest.get("signal_date", "?")

    issues = []
    notes  = []

    # ── 指标1：成交率 ─────────────────────────────────────────
    fill_rate = latest.get("fill_rate_pct", 100.0)
    if fill_rate < FILL_WARN_PCT:
        issues.append(f"成交率{fill_rate:.0f}%（应≥{FILL_WARN_PCT:.0f}%，"
                      f"仅成交{latest.get('fill_count',0)}/{latest.get('target_buy_count',0)}只）")
    else:
        notes.append(f"成交率{fill_rate:.0f}%")

    # ── 指标2：滑点（看历史趋势）─────────────────────────────
    recent_slips = []
    for rf in result_files[:SLIP_CONSEC]:
        try:
            r = json.loads(rf.read_text(encoding="utf-8"))
            recent_slips.append(abs(r.get("avg_slippage_pct", 0.0)))
        except Exception:
            pass

    avg_slip = latest.get("avg_slippage_pct", 0.0)
    max_slip = latest.get("max_slippage_pct", 0.0)
    consec_high = sum(1 for s in recent_slips if s > SLIP_WARN_PCT)

    if consec_high >= SLIP_CONSEC:
        issues.append(
            f"滑点连续{consec_high}次超{SLIP_WARN_PCT}%（最近均值{sum(recent_slips)/len(recent_slips):.3f}%）"
        )
    elif abs(avg_slip) > SLIP_WARN_PCT:
        notes.append(f"本次平均滑点{avg_slip:+.3f}%（单次，尚未连续）")
    else:
        notes.append(f"滑点{avg_slip:+.3f}%（max={max_slip:.3f}%）")

    # ── 指标3：全链路时延 ──────────────────────────────────────
    latency = latest.get("latency_min")
    if latency is not None:
        if latency > LATENCY_WARN:
            issues.append(f"链路时延{latency:.1f}min（超过{LATENCY_WARN:.0f}min阈值）")
        else:
            notes.append(f"链路时延{latency:.1f}min")
    else:
        notes.append("时延：本次未记录")

    # ── 风控拦截提示 ───────────────────────────────────────────
    blocked = latest.get("blocked", [])
    if blocked:
        notes.append(f"风控拦截{len(blocked)}笔：{[b.get('code','?') for b in blocked[:3]]}")

    summary = f"[{sig_date}] " + "  ".join(notes)
    if issues:
        return False, "执行质量告警：" + "；".join(issues) + "  |  " + summary
    return True, summary


def check_track_correlation() -> tuple[bool, str]:
    """监控Track A/B净值滚动相关性。动量风格崩溃时两轨会同步回撤。"""
    import re
    logs = Path("logs")
    a_files = sorted(logs.glob("paper_trade_2*.csv"))
    b_files = sorted(logs.glob("paper_trade_b_2*.csv"))
    if len(b_files) < 5:
        return True, "Track B数据不足，跳过"
    a_nav, b_nav = [], []
    for f in a_files:
        m = re.search(r'paper_trade_(\d{8})\.csv', f.name)
        if not m: continue
        df = pd.read_csv(f, encoding='utf-8-sig')
        tot = df[df['代码'].astype(str).str.contains('合计', na=False)]
        if tot.empty: continue
        val_col = [c for c in df.columns if '市值' in c or '当前' in c][0]
        a_nav.append({'date': pd.Timestamp(m.group(1)), 'val': float(str(tot.iloc[0][val_col]).replace(',',''))})
    for f in b_files:
        m = re.search(r'paper_trade_b_(\d{8})\.csv', f.name)
        if not m: continue
        df = pd.read_csv(f, encoding='utf-8-sig')
        tot = df[df['代码'].astype(str).str.contains('合计', na=False)]
        if tot.empty: continue
        val_col = [c for c in df.columns if '市值' in c or '当前' in c][0]
        b_nav.append({'date': pd.Timestamp(m.group(1)), 'val': float(str(tot.iloc[0][val_col]).replace(',',''))})
    a = pd.DataFrame(a_nav).set_index('date').sort_index()
    b = pd.DataFrame(b_nav).set_index('date').sort_index()
    common = a.index.intersection(b.index)
    if len(common) < 10:
        return True, "相关数据样本不足"
    corr = a['val'].pct_change()[common].corr(b['val'].pct_change()[common])
    if abs(corr) > 0.70:
        return False, f"Track A/B相关性{corr:.2f}（>0.70，动量同步风险高）"
    return True, f"Track A/B相关性{corr:.2f}（正常）"


def check_reconciliation() -> tuple[bool, str]:
    """检查QMT实际持仓与信号目标持仓是否一致。"""
    pos_file = Path("logs/qmt_positions_latest.json")
    sig_file = Path("data_store/meta/signal_a_latest.json")
    if not pos_file.exists():
        return True, "暂无QMT持仓数据（首次调仓后自动采集）"
    qmt = json.loads(pos_file.read_text(encoding="utf-8"))
    sig = json.loads(sig_file.read_text(encoding="utf-8"))
    qmt_codes = {c.split(".")[0] for c in qmt.get("positions",{}).keys()
                 if qmt["positions"][c].get("volume",0) > 0}
    sig_codes = set(sig.get("holdings", []))
    missing = sorted(sig_codes - qmt_codes)
    extra   = sorted(qmt_codes - sig_codes)
    if missing or extra:
        return False, f"对账差异：缺{len(missing)}只({missing[:3]}) 多{len(extra)}只({extra[:3]})"
    return True, f"对账一致({len(qmt_codes)}只)"


def check_rolling_beta() -> tuple[bool, str]:
    """
    计算策略过去12个月滚动单因子Beta（vs CSI 800）。
    Beta > 0.8 说明市场暴露偏高，触发预警。

    用历史宇宙净值文件；若不存在则用当前净值文件。
    Beta 本身用简单OLS，不依赖因子下载。
    """
    NAV_PATHS = [
        Path("logs/backtest_a4_nav_2016.csv"),
        Path("logs/backtest_a4_nav_hist_universe.csv"),
        Path("logs/backtest_a4_nav.csv"),
    ]
    BETA_WARN = 0.8

    # 找一个可用的净值文件
    nav_path = next((p for p in NAV_PATHS if p.exists()), None)
    if nav_path is None:
        return True, "未找到净值文件，跳过Beta检查"

    try:
        import statsmodels.api as sm

        nav = pd.read_csv(nav_path, index_col=0, parse_dates=True).squeeze()
        strat_ret = nav.resample("ME").last().pct_change().dropna()

        idx = load_meta("csi800_index")
        if idx.empty:
            return True, "CSI 800 指数数据缺失，跳过"
        idx["date"] = pd.to_datetime(idx["date"])
        mkt_ret = idx.set_index("date")["close"].resample("ME").last().pct_change().dropna()

        df = pd.concat([strat_ret, mkt_ret], axis=1).dropna()
        df.columns = ["strat", "mkt"]

        # 取最近12个月
        df_12m = df.tail(12)
        if len(df_12m) < 8:
            return True, f"近期数据不足（{len(df_12m)}月），跳过"

        X = sm.add_constant(df_12m["mkt"])
        m = sm.OLS(df_12m["strat"], X).fit()
        beta = m.params["mkt"]
        r2   = m.rsquared

        # 全样本滚动Beta分位（用于判断当前水位）
        roll_betas = []
        for i in range(12, len(df) + 1):
            w = df.iloc[i - 12 : i]
            Xw = sm.add_constant(w["mkt"])
            roll_betas.append(sm.OLS(w["strat"], Xw).fit().params["mkt"])
        pct_rank = sum(b < beta for b in roll_betas) / len(roll_betas) * 100

        msg = (f"滚动12月Beta={beta:.3f}（历史{pct_rank:.0f}%分位），"
               f"R²={r2:.2f}，基于{len(df_12m)}月数据")

        if beta > BETA_WARN:
            return False, f"⚠️ Beta偏高：{msg}"
        return True, msg

    except Exception as e:
        return True, f"Beta检查出错（不影响运行）：{e}"


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
        ("信号心跳(08:55)", check_signal_heartbeat),
        ("日线数据新鲜度",   check_daily_data_freshness),
        ("Track A 信号",    check_signal_a_freshness),
        ("Track B 信号",    check_signal_b_freshness),
        ("股票元数据",       check_stock_meta_freshness),
        ("CSI 指数成分",    check_csi_index_freshness),
        ("持仓自动对账",    check_reconciliation),
        ("Track A/B 相关性", check_track_correlation),
        ("执行质量监控",    check_execution_quality),
        ("策略滚动Beta",    check_rolling_beta),
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
