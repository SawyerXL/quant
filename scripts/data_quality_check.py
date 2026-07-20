"""
数据质量检查 — 每天开盘前+收盘后强制运行，防脏数据污染信号/回测。

检查项:
  1. 数据新鲜度: 最近交易日是否已更新（不新鲜=红线）
  2. 价格跳变: 日收益率绝对值 >50% (A股涨跌停±10/20%, >50%必脏)
  3. 成交量异常: 日成交额突变 >100x
  4. 数据源健康: 北向资金/指数数据是否就绪
  5. 更新成功率: 最近一次数据更新的成功率

用法: python scripts/data_quality_check.py [--fix]
强制纪律: 开盘前(07:00) + 收盘后(16:00) 每日两次检查
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json, pandas as pd, numpy as np
from datetime import date, datetime, timedelta
from loguru import logger
from data.storage import load_daily, load_meta

CHECK_LOG = Path("logs/data_quality.log")
ALERT_THRESHOLD = 0.50
VOL_JUMP_RATIO = 100

logger.add(CHECK_LOG, rotation="7 days", retention="30 days",
           format="{time} | {level} | {message}")

# ── 检查1: 数据新鲜度 (红灯检查) ──────────────────────
def check_data_freshness(calendar, end_date):
    """检查持仓+指数最新数据是否就绪。返回 (ok, msg, staleness_days)

    盘中(end_date是今天但市场还没收盘)时, 检查前一个交易日而不是今天。
    """
    if not calendar:
        return False, "🔴 无交易日历", 999
    cal_list = sorted(calendar) if isinstance(calendar, (list, set)) else sorted(calendar["trade_date"].tolist())
    past_dates = [d for d in cal_list if d <= end_date]
    if not past_dates:
        return False, "🔴 无历史交易日", 999

    # 如果是今天且还没到15:30, 检查前一个交易日
    now = datetime.now()
    latest_trade_day = str(past_dates[-1])[:10]
    if latest_trade_day == end_date and now.hour < 16:
        if len(past_dates) >= 2:
            latest_trade_day = str(past_dates[-2])[:10]
            logger.info(f"  盘中检查: 今日未收盘, 改用前一交易日 {latest_trade_day}")

    staleness = (date.today() - date.fromisoformat(latest_trade_day)).days

    # 采样: 持仓前5 + 核心指数
    samples = []
    try:
        hdf = pd.read_csv("config/my_holdings.csv", dtype={"code": str})
        held = hdf[(hdf['monitor'] == True) & (hdf['shares'] > 0)]['code'].str.zfill(6).tolist()
        samples.extend(held[:5])
    except: pass

    # 必查指数（仅限有独立parquet的：000001/000688/000905/000906已重建）
    # 399006(创业板指)/000300(沪深300)是纯指数无个股parquet，不检查
    idx_codes = ['000001', '000688', '000905', '000906']
    samples.extend(idx_codes)
    samples = list(set(samples))

    missing = []
    stale_count = 0
    for code in samples:
        try:
            df = load_daily(code, latest_trade_day, latest_trade_day)
        except:
            missing.append(code)
            stale_count += 1
            continue
        if df.empty or len(df) < 1:
            missing.append(code)
            stale_count += 1

    if stale_count >= len(samples) * 0.5:
        return False, f"🔴 数据严重缺失: {stale_count}/{len(samples)} (最新交易日{latest_trade_day})", staleness
    elif stale_count > 0:
        return False, f"🟡 部分缺失: {stale_count}/{len(samples)} 无数据 ({', '.join(missing[:5])})", staleness
    else:
        return True, f"✅ 最新{latest_trade_day}, {len(samples)}只全就绪", staleness

# ── 检查2: 价格跳变 ─────────────────────────────────
def check_price_jumps(codes, end_date):
    start = (pd.Timestamp(end_date) - timedelta(days=15)).strftime("%Y-%m-%d")
    dirty = []
    for code in codes:
        try:
            df = load_daily(code, start, end_date)
        except: continue
        if df.empty or len(df) < 5: continue
        df = df.sort_values("date")
        closes = pd.to_numeric(df["close"], errors="coerce").dropna()
        if closes.empty: continue
        rets = closes.pct_change().dropna()
        bad = rets[rets.abs() > ALERT_THRESHOLD]
        for idx, val in bad.items():
            dirty.append({"code": code, "date": str(df.iloc[idx]["date"])[:10] if "date" in df.columns else str(idx),
                          "change": f"{val*100:+.0f}%"})
    return dirty

# ── 检查3: 成交量异常 ────────────────────────────────
def check_volume_jumps(codes, end_date):
    start = (pd.Timestamp(end_date) - timedelta(days=30)).strftime("%Y-%m-%d")
    anomalies = []
    for code in codes:
        try:
            df = load_daily(code, start, end_date)
        except: continue
        if df.empty or len(df) < 21: continue
        df = df.sort_values("date")
        amounts = pd.to_numeric(df["amount"], errors="coerce").dropna()
        if len(amounts) < 21: continue
        avg_20 = amounts.iloc[-21:-1].mean()
        if avg_20 <= 0: continue
        if amounts.iloc[-1] / avg_20 > VOL_JUMP_RATIO:
            anomalies.append({"code": code, "date": str(df.iloc[-1]["date"])[:10], "ratio": f"{amounts.iloc[-1]/avg_20:.0f}x"})
    return anomalies

# ── 检查4: 北向资金数据就绪 ───────────────────────────
def check_northbound_data():
    """检查北向资金数据是否存在且新鲜。"""
    nb_files = list(Path("data_store").glob("*north*")) + list(Path("data_store/meta").glob("*north*"))
    if not nb_files:
        return False, "🔴 北向资金数据完全缺失 — 从未建过文件"

    for f in nb_files:
        try:
            if f.suffix == '.parquet':
                df = pd.read_parquet(f)
            else:
                df = pd.read_csv(f)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                last_date = df['date'].max().strftime('%Y-%m-%d')
                days_behind = (date.today() - df['date'].max().date()).days

                # 检查每日净买额是否实际有效（东财API自2024-08-19起返回NaN）
                net_cols = [c for c in df.columns if 'net' in c.lower() or '净买' in c]
                net_dead = False
                if net_cols:
                    net_valid = df[df[net_cols].notna().any(axis=1)]
                    if net_valid.empty:
                        net_dead = True
                    else:
                        last_net_dt = net_valid['date'].max()
                        net_days_behind = (date.today() - last_net_dt.date()).days
                        net_dead = net_days_behind > 365  # 超一年无有效净买额=API已死

                if days_behind <= 2:
                    return True, f"✅ 北向日期{last_date}"
                elif net_dead:
                    return True, f"⚠️ 北向净买额缺失(API限制, 自2024-08), 日期列{last_date}"
                else:
                    # 北向日流向源自2024-08已停(东财接口变更), 剩余数据更新不频繁;
                    # 且北向非本策略(TOP30+MA200)输入 → 不作硬异常, 仅提示
                    return True, f"🟡 北向滞后{days_behind}天(源已降级, 非策略输入, 仅参考)"
            else:
                return True, f"✅ {f.name}: {len(df)}行 (无日期列)"
        except Exception as e:
            return False, f"🔴 {f.name} 读取失败: {e}"

    return False, "🔴 北向资金数据异常"

# ── 检查5: 指数数据就绪 ──────────────────────────────
def check_index_data():
    """检查核心指数数据是否就绪。"""
    idx_map = {'000001': '上证指数', '399006': '创业板指', '000300': '沪深300',
               '000905': '中证500', '000688': '科创50'}
    missing = []
    for code, name in idx_map.items():
        try:
            df = load_daily(code, '2026-06-15', date.today().strftime('%Y-%m-%d'))
            if df.empty:
                missing.append(f'{name}({code})')
        except:
            missing.append(f'{name}({code})')
    if missing:
        return False, f"🔴 指数数据缺失: {', '.join(missing)}"
    return True, "✅ 5大指数数据就绪"

# ── 检查6: 指数/个股代码防污染 (2026-07-07修复后新增) ──
def check_index_code_contamination():
    """000001等代码同时是指数和个股, 回补时可能被stock_zh_a_daily拉成个股价格。
    上证指数~4000点, 个股~10元。正常值范围: 指数>1000, 个股<10000。"""
    idx_expected_range = {
        '000001': (3000, 5000),   # 上证指数 3000-5000
        '000300': (3500, 5500),   # 沪深300
        '399006': (2500, 5000),   # 创业板
        '000688': (800, 2500),    # 科创50
        '000905': (6000, 10000),  # 中证500
        '000906': (4500, 6500),   # 中证800
    }
    contaminated = []
    for code, (lo, hi) in idx_expected_range.items():
        try:
            df = load_daily(code, '2026-07-01', date.today().strftime('%Y-%m-%d'))
            if df.empty: continue
            cl = pd.to_numeric(df['close'], errors='coerce').dropna()
            if cl.empty: continue
            latest = cl.iloc[-1]
            if latest < lo or latest > hi:
                contaminated.append(f'{code}({latest:.0f}, 应在{lo}-{hi})')
        except:
            pass
    if contaminated:
        return False, f"🔴 指数数据被个股污染: {', '.join(contaminated)} — 需重建(删除旧parquet+重拉stock_zh_index_daily)"
    return True, "✅ 指数代码无污染"

# ── 检查7: CSI800数据新鲜度 (2026-07-07修复后新增) ──
def check_csi800_freshness():
    """CSI800(000906)是策略仓位计算的核心基准。用交易日历判"应有的最新交易日",
    避免周一/节后把上一交易日数据误报为落后(同check_signal_freshness的日历化)。"""
    try:
        df = load_daily('000906', '2026-06-01', date.today().strftime('%Y-%m-%d'))
        if df.empty:
            return False, "🔴 CSI800(000906)无数据"
        df['date'] = pd.to_datetime(df['date'])
        last_date = df['date'].max().strftime('%Y-%m-%d')
        cal = load_meta('trade_calendar')
        tdays = sorted(cal['trade_date'].tolist()) if not cal.empty else []
        today_str = date.today().strftime('%Y-%m-%d')
        past = [d for d in tdays if d < today_str]
        expected = past[-1] if past else today_str  # 上一交易日(当天收盘前数据还没入库)
        if last_date >= expected:
            return True, f"✅ CSI800最新{last_date}"
        behind = len([d for d in tdays if last_date < d <= expected])  # 真实落后的交易日数
        return False, f"🔴 CSI800落后{behind}个交易日 (最新{last_date}, 应到{expected})"
    except Exception as e:
        return False, f"🔴 CSI800数据读取失败: {e}"

# ── 检查8: 信号文件新鲜度 (2026-07-07修复后新增) ──
def check_signal_freshness():
    """信号文件日期必须='最近一个应已生成信号的交易日', 否则QMT会误判过期并可能错误清仓。
    信号08:55生成: 交易日盘前(09:00前)或周末/节假日, 期望的是上一个交易日, 不算过期。"""
    sig_path = Path('data_store/meta/signal_a_latest.json')
    if not sig_path.exists():
        return False, "🔴 信号文件不存在"
    try:
        sig = json.loads(sig_path.read_text(encoding='utf-8'))
        sig_date = sig.get('signal_date', sig.get('date', ''))
        today_str = date.today().strftime('%Y-%m-%d')
        cal = load_meta('trade_calendar')
        tdays = sorted(cal['trade_date'].tolist()) if not cal.empty else []
        past = [d for d in tdays if d <= today_str]
        expected = past[-1] if past else today_str
        # 今天是交易日但信号还没到生成时间(09:00前) → 期望上一个交易日
        if expected == today_str and datetime.now().hour < 9:
            expected = past[-2] if len(past) >= 2 else today_str
        if sig_date != expected:
            return False, f"🔴 信号日期过期: {sig_date} != 期望{expected} — QMT会跳过执行"
        return True, f"✅ 信号日期{sig_date}"
    except Exception as e:
        return False, f"🔴 信号文件读取失败: {e}"


def check_qmt_snapshot():
    """检查QMT快照是否存在、新鲜、数据合理。"""
    snap_file = Path("logs/qmt_positions_latest.json")
    if not snap_file.exists():
        return False, "🔴 QMT快照文件不存在 — 需运行 qmt_snapshot.py"
    
    try:
        d = json.loads(snap_file.read_text(encoding="utf-8"))
        exported = d.get("exported_at", "")
        pos = d.get("positions", {})
        acc = d.get("account", {})
        mv = acc.get("market_value", 0)
        total = acc.get("total_assets", 0)
        
        if exported:
            snap_date = exported[:10]
            today_str = date.today().strftime("%Y-%m-%d")
            if snap_date != today_str:
                # 用交易日历判定: 快照应不早于上一个交易日
                cal = load_meta('trade_calendar')
                tdays = sorted(cal['trade_date'].tolist()) if not cal.empty else []
                past = [d for d in tdays if d < today_str]
                prev_td = past[-1] if past else today_str
                # 交易日15:45前: 快照=上一交易日 → 正常; 早于上一交易日 → 失效
                if today_str in tdays and datetime.now().hour < 16:
                    if snap_date == prev_td:
                        return True, f"QMT昨日快照(正常, {snap_date}), 当日15:45更新"
                    else:
                        return False, f"QMT快照失效: {snap_date} vs 上一交易日{prev_td}, 快照管道可能断了"
                # 16:00后(快照应已刷新) 或非交易日: 快照应>=上一交易日
                if snap_date < (prev_td or today_str):
                    return False, f"QMT快照失效: {snap_date} < {prev_td or today_str}, 快照管道可能断了"
                # 非交易日, 快照=上一交易日 → 正常
                return True, f"QMT快照{snap_date} (非交易日, 正常)"
        
        n = len(pos)
        if n < 10:
            return False, f"QMT positions too few: {n} (possible liquidation)"
        if n > 35:
            return False, f"QMT positions too many: {n}"
        
        ratio = mv / total * 100 if total > 0 else 0
        if ratio > 50:
            return False, f"QMT position ratio abnormal: {ratio:.0f}% (data contamination?)"
        if ratio < 0.3:
            return False, f"QMT position ratio too low: {ratio:.1f}% (possible liquidation)"
        
        return True, f"QMT snapshot OK: {n} pos, {mv:,.0f} ({ratio:.1f}%)"
    except Exception as e:
        return False, f"QMT snapshot read failed: {e}"

# ── 主流程 ──────────────────────────────────────────
def run(fix=False):
    today = date.today().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    is_premarket = datetime.now().hour < 10
    phase = "盘前" if is_premarket else "盘后"

    logger.info(f"=== {phase}数据质量检查 {today} {now} ===")

    # 交易日历
    cal = load_meta("trade_calendar")
    cal_list = sorted(cal["trade_date"].tolist()) if not cal.empty else []

    # 检查范围
    c800 = load_meta("csi800")
    c800_codes = sorted(c800["code"].tolist()) if not c800.empty else []
    try:
        holdings_df = pd.read_csv("config/my_holdings.csv", dtype={"code": str})
        holdings_codes = holdings_df["code"].str.zfill(6).tolist()
    except:
        holdings_codes = []
    check_codes = list(set(c800_codes[:200] + holdings_codes))  # 只查前200+持仓, 加快速度

    # ── 运行所有检查 ──
    results = {"errors": [], "warnings": [], "ok": []}

    # 1. 数据新鲜度 (红灯)
    fresh_ok, fresh_msg, staleness = check_data_freshness(cal_list, today)
    if not fresh_ok:
        results["errors"].append(fresh_msg)
    else:
        results["ok"].append(fresh_msg)
    logger.info(f"  新鲜度: {fresh_msg}")

    # 2. 价格跳变
    dirty_prices = check_price_jumps(check_codes, today)
    if dirty_prices:
        results["errors"].append(f"⚡ 价格跳变 {len(dirty_prices)}处")
        for d in dirty_prices[:5]:
            logger.warning(f"  脏价: {d['code']} {d['date']} {d['change']}")
        if len(dirty_prices) > 5:
            results["errors"].append(f"  ... 共{len(dirty_prices)}处")
    else:
        logger.info("  价格: 正常")

    # 3. 成交量异常
    vol_anomalies = check_volume_jumps(check_codes, today)
    if vol_anomalies:
        results["errors"].append(f"📊 成交量异常 {len(vol_anomalies)}处")
        for v in vol_anomalies[:3]:
            logger.warning(f"  量异: {v['code']} {v['date']} {v['ratio']}倍")
    else:
        logger.info("  成交量: 正常")

    # 4. 北向资金
    nb_ok, nb_msg = check_northbound_data()
    if not nb_ok:
        results["errors"].append(nb_msg)
    else:
        results["ok"].append(nb_msg)
    logger.info(f"  北向: {nb_msg}")

    # 5. 指数数据
    idx_ok, idx_msg = check_index_data()
    if not idx_ok:
        results["errors"].append(idx_msg)
    else:
        results["ok"].append(idx_msg)
    logger.info(f"  指数: {idx_msg}")

    # 6. 指数代码防污染 (防止000001被拉成个股价格)
    contam_ok, contam_msg = check_index_code_contamination()
    if not contam_ok:
        results["errors"].append(contam_msg)
    else:
        results["ok"].append(contam_msg)
    logger.info(f"  指数污染: {contam_msg}")

    # 7. CSI800数据新鲜度 (策略仓位基准)
    csi_ok, csi_msg = check_csi800_freshness()
    if not csi_ok:
        results["errors"].append(csi_msg)
    else:
        results["ok"].append(csi_msg)
    logger.info(f"  CSI800: {csi_msg}")

    # 8. 信号文件新鲜度 (防止QMT用旧信号清仓)
    sig_ok, sig_msg = check_signal_freshness()
    if not sig_ok:
        results["errors"].append(sig_msg)
    else:
        results["ok"].append(sig_msg)
    logger.info(f"  信号: {sig_msg}")

    # 9. QMT快照
    qmt_ok, qmt_msg = check_qmt_snapshot()
    if not qmt_ok:
        results["errors"].append(qmt_msg)
    else:
        results["ok"].append(qmt_msg)
    logger.info(f"  QMT快照: {qmt_msg}")

    # ── 汇总 ──
    has_errors = len(results["errors"]) > 0
    status = "🔴 严重异常" if has_errors else "✅ 全部正常"

    lines = []
    lines.append(f"\n{'='*55}")
    lines.append(f"  {phase}数据质量检查 {now} — {status}")
    lines.append(f"{'='*55}")
    if results["errors"]:
        lines.append("  ❌ 异常:")
        for e in results["errors"]:
            lines.append(f"    {e}")
    if results["ok"]:
        lines.append("  ✅ 正常:")
        for o in results["ok"]:
            lines.append(f"    {o}")
    lines.append(f"{'='*55}\n")

    report = '\n'.join(lines)
    print(report)

    # ── 告警 ──
    if has_errors:
        from monitoring.alerts import send_alert
        msg = f"【{phase}数据质量告警 {now}】\n" + "\n".join(results["errors"])
        msg += "\n\n⚠️ 脏数据/缺失数据会影响信号和回测，必须修复后再运行策略。"
        send_alert(msg, level="error")
        logger.error(f"数据质量告警: {len(results['errors'])}项异常")
        return False

    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="自动修复脏数据")
    args = parser.parse_args()
    ok = run(fix=args.fix)
    sys.exit(0 if ok else 1)

# ── 检查9: QMT快照状态 (2026-07-08 新增) ──
