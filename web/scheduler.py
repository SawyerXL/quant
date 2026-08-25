"""APScheduler jobs — auto-refresh snapshots on trade days."""
import threading
from datetime import date, datetime
from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

_scheduler: BackgroundScheduler | None = None


def _is_trade_day() -> bool:
    """Check if today is a trade day."""
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from data.storage import load_meta
        cal = load_meta("trade_calendar")
        today = str(date.today())
        return today in cal["trade_date"].values
    except Exception:
        return True  # Default to running on error


def _refresh_regime():
    """Refresh market regime snapshot."""
    if not _is_trade_day():
        return
    try:
        from web.services.regime_service import refresh_regime
        refresh_regime()
        logger.info("Regime snapshot refreshed")
    except Exception as e:
        logger.error(f"Regime refresh failed: {e}")


def _refresh_ma10():
    """Refresh MA10 triggers snapshot."""
    if not _is_trade_day():
        return
    try:
        from web.services.portfolio_service import get_ma10_triggers
        from web.snapshots import write_snapshot
        # Force re-compute by calling directly (bypasses cache)
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.check_ma10_triggers import run as run_ma10
        import pandas as pd
        df = run_ma10(send=False)
        if not df.empty:
            triggers = df.fillna("").to_dict(orient="records")
            for t in triggers:
                for k, v in list(t.items()):
                    if hasattr(v, "item"):
                        t[k] = v.item()
            judged = [t for t in triggers if t.get("verdict") and t["verdict"] != "待判定"]
            effective = [t for t in judged if t.get("verdict") == "有效"]
            stats = {
                "total": len(triggers), "judged": len(judged),
                "effective": len(effective),
                "effective_rate": round(len(effective)/len(judged)*100, 1) if judged else 0,
            }
            write_snapshot("ma10_triggers", {"triggers": triggers, "stats": stats},
                          source="check_ma10_triggers.run")
        logger.info("MA10 triggers snapshot refreshed")
    except Exception as e:
        logger.error(f"MA10 refresh failed: {e}")


def _refresh_signal():
    """Refresh signal snapshot (force re-read from source)."""
    if not _is_trade_day():
        return
    try:
        from web.services.signal_service import get_latest_signal
        get_latest_signal(force=True)  # Force re-read from signal_a_latest.json
        logger.info("Signal snapshot refreshed")
    except Exception as e:
        logger.error(f"Signal refresh failed: {e}")


def _refresh_candidates():
    """Refresh candidates snapshot at market open."""
    if not _is_trade_day():
        return
    try:
        from web.services.signal_service import get_candidates
        from web.snapshots import write_snapshot
        cands = get_candidates(limit=30)
        write_snapshot("candidates", cands, source="scheduler-9am")
        from web.services.signal_service import get_latest_signal
        get_latest_signal(force=True)
        logger.info(f"Candidates refreshed: {len(cands)} stocks")
    except Exception as e:
        logger.error(f"Candidates refresh failed: {e}")


def _refresh_overnight():
    """Refresh overnight US market snapshot (for morning briefing)."""
    if not _is_trade_day():
        return
    try:
        from scripts.overnight_market import get_overnight_analysis
        from web.snapshots import write_snapshot
        ov = get_overnight_analysis()
        if ov and ov.get("sp500"):
            write_snapshot("overnight", ov, source="scheduler-8am")
            logger.info(f"Overnight refreshed: S&P {ov.get('sp500_chg', 0):+.2f}%")
    except Exception as e:
        logger.error(f"Overnight refresh failed: {e}")


def _refresh_flow_cache():
    """Refresh TOP60 flow cache for candidate selection."""
    if not _is_trade_day():
        return
    try:
        import sys, os, io
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        # Suppress stdout noise
        old = os.dup(1)
        os.close(1)
        os.open('/dev/null', os.O_WRONLY)
        try:
            from scripts.sector_flow_cache import run as run_flow
            run_flow()
        finally:
            os.dup2(old, 1)
            os.close(old)
        logger.info("Flow cache refreshed")
    except Exception as e:
        logger.error(f"Flow cache refresh failed: {e}")


def _check_t_signals():
    """Intraday T+0 signal check + Feishu push."""
    try:
        from datetime import datetime, time as dtime
        now = datetime.now()
        if now.weekday() >= 5:
            return
        t = now.time()
        if not (dtime(9, 35) <= t <= dtime(14, 50)):
            return
        from web.services.tservice import get_t_signals
        result = get_t_signals(is_admin=True, user_id="")
        sigs = result.get("signals", [])
        if not sigs:
            return
        # Push alerts
        from web.services.push_notify import send_text_feishu
        lines = ["做T触发提醒:"]
        for s in sigs:
            lines.append(f"{s['direction']} {s['name']}({s['code']}) {s['chg_pct']:+.1f}% → {s['action']}")
        send_text_feishu("\n".join(lines))
        logger.info(f"T-signal push: {len(sigs)} signals")
    except Exception as e:
        logger.error(f"T-signal check failed: {e}")


def _push_daily_report():
    """Push daily market report to Feishu."""
    if not _is_trade_day():
        return
    try:
        from web.services.push_notify import push_daily_market_report
        ok = push_daily_market_report()
        logger.info(f"Feishu report push: {'OK' if ok else 'skipped (no webhook or failed)'}")
    except Exception as e:
        logger.error(f"Feishu push failed: {e}")


def _update_daily_data():
    """Pull latest daily bars for key indices and backfill regime history."""
    if not _is_trade_day():
        return
    try:
        import sys, os, io
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

        # 1. Pull latest 上证指数 daily data
        import akshare as ak
        import pandas as pd
        from data.storage import save_daily, load_daily
        from datetime import date

        today = str(date.today())
        df = ak.stock_zh_index_daily(symbol="sh000001")
        df['date'] = df['date'].astype(str)

        saved = 0
        for _, row in df.iterrows():
            d = row['date']
            if d < '2026-08-01': continue
            existing = load_daily('000001', d, d)
            if existing.empty:
                new_row = pd.DataFrame([{'date': d, 'open': row['open'], 'high': row['high'],
                    'low': row['low'], 'close': row['close'], 'volume': row['volume'],
                    'amount': row.get('amount', 0), 'pct_chg': row.get('pct_chg', 0)}])
                save_daily('000001', new_row)
                saved += 1

        if saved:
            logger.info(f"Daily data: saved {saved} missing 上证 bars")

        # 2. Re-backfill regime history with latest data
        from web.snapshots import write_snapshot
        import json

        cal_df = pd.read_parquet(Path(__file__).parent.parent / "data_store" / "meta" / "trade_calendar.parquet")
        all_trade = sorted(cal_df['trade_date'].astype(str).tolist())
        past = [d for d in all_trade if "2024-01-01" <= d <= today]

        dfs = []
        for y in [2024, 2025, 2026]:
            df = load_daily('000001', f'{y}-01-01', f'{y}-12-31')
            if not df.empty: dfs.append(df)
        sh = pd.concat(dfs)
        sh['date'] = pd.to_datetime(sh['date'])
        sh = sh.set_index('date').sort_index()
        close = sh['close'].dropna()
        ma200 = close.rolling(200).mean()
        ma60 = close.rolling(60).mean()

        count = 0
        for d in past[-30:]:  # Only refresh last 30 days
            dt = pd.Timestamp(d)
            if dt not in close.index: continue
            c = float(close[dt]); m200 = ma200[dt]
            if pd.isna(m200): continue
            dist_pct = round((c/m200 - 1)*100, 2)
            cons = 0
            idx_pos = all_trade.index(d) if d in all_trade else -1
            for j in range(idx_pos, -1, -1):
                pdt = pd.Timestamp(all_trade[j])
                if pdt not in close.index or pd.isna(ma200[pdt]): break
                if close[pdt] < ma200[pdt]: cons += 1
                else: break
            m60v = ma60[dt]
            ma60_below = not pd.isna(m60v) and float(m60v) < float(m200)
            if cons >= 20 or ma60_below: v = "bear"
            elif cons >= 5: v = "danger"
            elif cons >= 2: v = "warning"
            else: v = "normal"
            old = Path(f"/root/quant/data_store/web_cache/regime_{d}.json")
            if old.exists(): old.unlink()
            write_snapshot("regime", {"verdict": v, "sh_close": round(c,1),
                "sh_days_below_ma200": cons, "ma200_dist_pct": dist_pct, "date": d},
                source="scheduler-daily", snapshot_date=d)
            count += 1

        if saved or count:
            logger.info(f"Daily update: {saved} bars saved, {count} regime snapshots refreshed")
    except Exception as e:
        logger.error(f"Daily update failed: {e}")


def _cleanup_old_snapshots():
    """Remove snapshots older than 90 days to keep cache lean."""
    try:
        from pathlib import Path
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=90)).strftime('%Y-%m-%d')
        cache_dir = Path(__file__).parent.parent / "data_store" / "web_cache"
        count = 0
        for f in cache_dir.glob("regime_*.json"):
            if "_latest" in f.name: continue
            d = f.name.replace("regime_", "").replace(".json", "")
            if d < cutoff:
                f.unlink()
                count += 1
        if count:
            logger.info(f"Cleaned up {count} old snapshots (before {cutoff})")
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    # Market open + margin data available: 09:07 weekdays
    _scheduler.add_job(_refresh_overnight, "cron", day_of_week="mon-fri", hour=8, minute=7,
                       id="overnight_pre")
    _scheduler.add_job(_refresh_regime, "cron", day_of_week="mon-fri", hour=9, minute=7,
                       id="regime_morning")
    # Candidates refresh at market open: 09:03 weekdays
    _scheduler.add_job(_refresh_candidates, "cron", day_of_week="mon-fri", hour=9, minute=3,
                       id="candidates_open")
    # Post-close: 15:05 weekdays
    _scheduler.add_job(_refresh_regime, "cron", day_of_week="mon-fri", hour=15, minute=7,
                       id="regime_post")
    _scheduler.add_job(_refresh_ma10, "cron", day_of_week="mon-fri", hour=15, minute=11,
                       id="ma10_post")
    _scheduler.add_job(_refresh_signal, "cron", day_of_week="mon-fri", hour=15, minute=17,
                       id="signal_post")
    # Flow cache refresh: 14:55
    _scheduler.add_job(_refresh_flow_cache, "cron", day_of_week="mon-fri", hour=14, minute=55,
                       id="flow_cache")
    # Push daily report to Feishu: 15:20
    _scheduler.add_job(_push_daily_report, "cron", day_of_week="mon-fri", hour=15, minute=23,
                       id="feishu_report")
    # Daily data update + history backfill: 16:00 (after akshare publishes daily bars)
    _scheduler.add_job(_update_daily_data, "cron", day_of_week="mon-fri", hour=16, minute=7,
                       id="daily_data")
    # T+0 signal check every 5 min during trading: 9:35-14:50
    _scheduler.add_job(_check_t_signals, "cron", day_of_week="mon-fri", minute="*/5",
                       hour="9-14", id="t_signals")
    # Cleanup old snapshots weekly
    _scheduler.add_job(_cleanup_old_snapshots, "cron", day_of_week="sat", hour=3, minute=0,
                       id="cleanup")
    _scheduler.start()
    logger.info("Scheduler: 09:03/07 morning, 14:55 flow, 15:07-23 close+push, 16:07 data, sat cleanup")


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
