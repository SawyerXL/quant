"""Backtest service — wraps backtest_engine. ThreadPool job queue."""
import sys, os, json, uuid, threading
from pathlib import Path
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from config.settings import ROOT, DATA_STORE
from web.db import SessionLocal
from web.models import BacktestJob

_executor = ThreadPoolExecutor(max_workers=2)
_jobs: dict[str, dict] = {}

# UI key → BacktestConfig field mapping
_KEY_MAP = {
    "abs_stop": "absolute_stop",
    "take_profit_tiers": None,  # special handling: split to take_profit_1/2
    "industry_cap": None,  # not a BacktestConfig field, ignored
    "pool_size": "pool_size",
    "overheat_mode": "overheat_mode",
    "trailing_stop": "trailing_stop",
    "ma_exit_days": "ma_exit_days",
    "max_position_pct": "max_position_pct",
    "rebalance_freq": "rebalance_freq",
}


def _translate_config(ui_config: dict) -> dict:
    """Translate UI parameter names to BacktestConfig field names."""
    cfg = {}
    for ui_key, value in ui_config.items():
        real_key = _KEY_MAP.get(ui_key, ui_key)
        if real_key is None:
            if ui_key == "take_profit_tiers" and isinstance(value, list) and len(value) >= 2:
                cfg["take_profit_1"] = float(value[0]) / 100
                cfg["take_profit_2"] = float(value[1]) / 100
            elif ui_key == "take_profit_tiers" and isinstance(value, str):
                parts = [float(x.strip()) for x in value.split(",") if x.strip()]
                if len(parts) >= 2:
                    cfg["take_profit_1"] = parts[0] / 100
                    cfg["take_profit_2"] = parts[1] / 100
            continue
        # Percentage fields: UI sends -12, config expects -0.12
        if real_key in ("absolute_stop", "trailing_stop"):
            if isinstance(value, (int, float)) and value < 0 and value < -1:
                value = value / 100
        cfg[real_key] = value
    return cfg


def get_default_config() -> dict:
    """Return DEFAULT_CONFIG as dict for the UI form."""
    try:
        from scripts.backtest_config import BacktestConfig
        cfg = BacktestConfig()
        return {
            "pool_size": cfg.pool_size,
            "overheat_mode": cfg.overheat_mode,
            "abs_stop": cfg.abs_stop,
            "trailing_stop": cfg.trailing_stop,
            "ma_exit_days": cfg.ma_exit_days,
            "take_profit_tiers": cfg.take_profit_tiers,
            "rebalance_freq": cfg.rebalance_freq,
            "max_position_pct": cfg.max_position_pct,
            "industry_cap": cfg.industry_cap,
        }
    except Exception as e:
        return {"error": str(e)}


def submit_job(user_id: str, name: str, config: dict, config_b: dict | None = None) -> str:
    """Submit a backtest job. Returns job_id."""
    job_id = uuid.uuid4().hex[:12]

    # Save to DB
    db = SessionLocal()
    try:
        job = BacktestJob(
            id=job_id, user_id=user_id, name=name,
            config_json=json.dumps(config),
            config_b_json=json.dumps(config_b) if config_b else None,
            status="queued",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    _jobs[job_id] = {"status": "queued", "progress": 0, "result": None, "error": None}
    _executor.submit(_run_backtest_job, job_id, config, config_b)
    return job_id


def get_job(job_id: str, user_id: str | None = None) -> dict:
    """Get job status and result. Optionally scoped to user."""
    j = _jobs.get(job_id)
    if j:
        if user_id:
            # Verify ownership from DB
            db = SessionLocal()
            try:
                bj = db.query(BacktestJob).filter(BacktestJob.id == job_id, BacktestJob.user_id == user_id).first()
                if not bj:
                    return {"status": "unknown", "error": "Job not found"}
            finally:
                db.close()
        return j
    # Try DB
    db = SessionLocal()
    try:
        q = db.query(BacktestJob).filter(BacktestJob.id == job_id)
        if user_id:
            q = q.filter(BacktestJob.user_id == user_id)
        bj = q.first()
        if bj:
            return {
                "status": bj.status, "progress": bj.progress,
                "result": json.loads(bj.result_json) if bj.result_json else None,
                "error": bj.error_msg,
            }
    finally:
        db.close()
    return {"status": "unknown", "error": "Job not found"}


def list_jobs(user_id: str) -> list[dict]:
    """List user's backtest jobs."""
    db = SessionLocal()
    try:
        jobs = db.query(BacktestJob).filter(
            BacktestJob.user_id == user_id
        ).order_by(BacktestJob.created_at.desc()).limit(20).all()
        return [{
            "id": j.id, "name": j.name, "status": j.status,
            "created_at": str(j.created_at)[:19] if j.created_at else "",
            "progress": j.progress,
        } for j in jobs]
    finally:
        db.close()


def _run_backtest_job(job_id: str, config: dict, config_b: dict | None):
    """Execute backtest in thread."""
    j = _jobs.get(job_id, {})
    j["status"] = "running"
    j["progress"] = 10

    try:
        from scripts.backtest_config import BacktestConfig
        from scripts.backtest_engine import run_backtest, calc_metrics
        from scripts.run_backtest_a import load_panels
        from scripts.run_backtest_a2 import _make_rebal_dates
        from data.storage import load_meta

        j["progress"] = 20

        # Build config
        cfg = BacktestConfig(**_translate_config(config))

        # Load data
        cal = load_meta("trade_calendar")
        cal_dates = sorted(cal["trade_date"].tolist())
        start = cal_dates[0]  # First available trade date
        end = str(date.today())  # Use today — panel loads whatever data exists
        rebal_dates = _make_rebal_dates(
            [d for d in cal_dates if start <= d <= end],
            cfg.rebalance_freq,
        )

        j["progress"] = 30

        # Load universe
        c800 = load_meta("csi800")
        codes = sorted(c800["code"].tolist())[:100]  # Limit for speed
        panel, ap = load_panels(codes, start, end)

        j["progress"] = 50

        # Load index
        idx = load_meta("csi800_index")
        idx_c = idx.set_index("date")["close"].sort_index()

        j["progress"] = 60

        # Run backtest
        nav, diagnostics = run_backtest(panel, ap, rebal_dates, cfg, idx_c)
        metrics = calc_metrics(nav, "Personal")

        j["progress"] = 90

        # Benchmark: equal-weight buy-and-hold of the panel
        bench_nav = panel.mean(axis=1)
        bench_nav = (1 + bench_nav.pct_change().fillna(0)).cumprod()
        # Align benchmark dates with strategy nav
        common_dates = nav.index.intersection(bench_nav.index)

        result = {
            "metrics": metrics,
            "diagnostics": diagnostics,
            # Every ~10 trading days for smooth curve, plus last point
            "nav": [[str(d.date())[:10], float(nav[d]), float(bench_nav.get(d, float(nav[d])))]
                    for i, d in enumerate(nav.index) if i % 10 == 0 or i == len(nav.index) - 1],
            "start": str(nav.index[0].date()),
            "end": str(nav.index[-1].date()),
        }

        j["result"] = result
        j["status"] = "done"
        j["progress"] = 100

        # Update DB
        db = SessionLocal()
        try:
            bj = db.query(BacktestJob).filter(BacktestJob.id == job_id).first()
            if bj:
                bj.status = "done"
                bj.progress = 100
                bj.result_json = json.dumps(result, default=str)
                bj.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    except Exception as e:
        j["status"] = "error"
        j["error"] = str(e)
        # Update DB
        db = SessionLocal()
        try:
            bj = db.query(BacktestJob).filter(BacktestJob.id == job_id).first()
            if bj:
                bj.status = "error"
                bj.error_msg = str(e)
                db.commit()
        finally:
            db.close()


def run_backtest_inline(config: dict) -> dict:
    """Run a single backtest and return metrics (for grid sweeps)."""
    from scripts.backtest_config import BacktestConfig
    from scripts.backtest_engine import run_backtest, calc_metrics
    from scripts.run_backtest_a import load_panels
    from scripts.run_backtest_a2 import _make_rebal_dates
    from data.storage import load_meta

    cfg = BacktestConfig(**_translate_config(config))
    cal = load_meta("trade_calendar")
    cal_dates = sorted(cal["trade_date"].tolist())
    start = cal_dates[0]
    end = max(d for d in cal_dates if d <= str(date.today()))
    rebal_dates = _make_rebal_dates(
        [d for d in cal_dates if start <= d <= end], cfg.rebalance_freq)

    c800 = load_meta("csi800")
    codes = sorted(c800["code"].tolist())[:100]
    panel, ap = load_panels(codes, start, end)
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()

    nav, diagnostics = run_backtest(panel, ap, rebal_dates, cfg, idx_c)
    metrics = calc_metrics(nav, "grid")
    bench_nav = panel.mean(axis=1)
    bench_nav = (1 + bench_nav.pct_change().fillna(0)).cumprod()
    return {
        "metrics": metrics,
        "nav_sample": [[str(d.date())[:10], float(nav[d]), float(bench_nav.get(d, float(nav[d])))]
            for i, d in enumerate(nav.index) if i % 10 == 0 or i == len(nav.index) - 1],
    }


def submit_grid(user_id: str, param: str, values: list, base_config: dict) -> str:
    """Submit a grid sweep job. Returns job_id."""
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "queued", "progress": 0, "result": None, "error": None,
                     "grid_param": param, "grid_values": values}

    db = SessionLocal()
    try:
        job = BacktestJob(
            id=job_id, user_id=user_id,
            name=f"Grid: {param}",
            config_json=json.dumps({"param": param, "values": values, "base": base_config}),
            status="queued",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    _executor.submit(_run_grid, job_id, param, values, base_config)
    return job_id


def _run_grid(job_id: str, param: str, values: list, base_config: dict):
    """Execute grid sweep."""
    j = _jobs.get(job_id, {})
    j["status"] = "running"
    results = []
    total = len(values)

    for i, v in enumerate(values):
        cfg = {**base_config, param: v}
        try:
            r = run_backtest_inline(cfg)
            m = r["metrics"]
            results.append({
                "param": param, "value": v,
                "annual": m.get("年化收益率", "—"),
                "sharpe": m.get("夏普比率", "—"),
                "mdd": m.get("最大回撤", "—"),
                "win_rate": m.get("月度胜率", "—"),
            })
        except Exception as e:
            results.append({"param": param, "value": v, "error": str(e)})
        j["progress"] = int((i + 1) / total * 100)

    j["result"] = {"param": param, "results": results}
    j["status"] = "done"
    j["progress"] = 100

    db = SessionLocal()
    try:
        bj = db.query(BacktestJob).filter(BacktestJob.id == job_id).first()
        if bj:
            bj.status = "done"
            bj.progress = 100
            bj.result_json = json.dumps(j["result"], default=str)
            bj.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
