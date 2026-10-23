"""只读监控摘要（2026-10-22，吸收 TickFlow 的四类监控组织方式）。

四类监控映射到我们已有工件，全部只读：
  strategy  -> 健康行 live_reconcile / tier_pos / cb_implementation
  signal    -> signal_a_g0/g1 目标 + CB 最新信号
  price     -> 最新 bar / QDII 溢价 / US 纸面腿
  anomaly   -> 健康行 alerts / audit / qfq_drift / 负持仓
不触发任何下单或写库。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])
ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str):
    p = ROOT.joinpath(*parts)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest(pattern: str):
    files = sorted(ROOT.glob(pattern))
    return files[-1] if files else None


def _read_file(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


@router.get("/summary")
def summary():
    health = _read("logs", "daily_health_line.json") or {}
    reconcile = health.get("live_reconcile") or {}
    g0 = _read("data_store", "meta", "signal_a_g0.json") or {}
    g1 = _read("data_store", "meta", "signal_a_g1.json") or {}
    cb = _read("data_store", "meta", "signal_cb_latest.json") or {}
    pos_file = _latest("logs/qmt_positions_*.json")
    positions = _read_file(pos_file) if pos_file else None
    orders_file = _latest("logs/qmt_orders_*.json")
    orders = _read_file(orders_file) if orders_file else None
    us = _read("logs", "us_track_state.json") or {}

    def leg(sig: dict):
        return {"signal_date": sig.get("signal_date"),
                "holdings": len(sig.get("holdings") or []),
                "buy": len(sig.get("buy") or []),
                "sell": len(sig.get("sell") or []),
                "regime": sig.get("regime"),
                "position_ratio": sig.get("position_ratio")}

    return {
        "as_of": health.get("ts"),
        "strategy": {
            "live_reconcile": {"status": reconcile.get("status"),
                               "snapshot": reconcile.get("snapshot"),
                               "cb_target": reconcile.get("cb_target_count"),
                               "cb_implementation": reconcile.get("cb_implementation"),
                               "stock_leg_pos": reconcile.get("stock_leg_pos"),
                               "tier_pos": reconcile.get("tier_pos"),
                               "regime": reconcile.get("sig_regime")},
            "tunnel": health.get("tunnel"),
            "snapshot_state": health.get("snapshot_state"),
            "cb_monthly_verify": health.get("cb_monthly_verify"),
            "cron_install": health.get("cron_install"),
        },
        "signal": {
            "stock_g0": leg(g0), "stock_g1": leg(g1),
            "cb": {"signal_date": cb.get("signal_date") or health.get("cb_signal_date"),
                   "holdings": len(cb.get("holdings") or []) if isinstance(cb.get("holdings"), list) else cb.get("holdings"),
                   "buy": cb.get("buy"), "sell": cb.get("sell")},
        },
        "price": {
            "latest_bar": health.get("latest_bar"),
            "bar_fresh": health.get("bar_fresh"),
            "qdii_premium": health.get("qdii_premium"),
            "us_track": health.get("us_track") or us,
        },
        "anomaly": {
            "alerts": health.get("alerts") or [],
            "audit": health.get("audit"),
            "qfq_drift": health.get("qfq_drift"),
            "negative_positions": health.get("negative_positions"),
            "cb_unknown_gaps": health.get("cb_unknown_gaps"),
        },
        "paper": {
            "satellite_100w5w": health.get("satellite_100w5w"),
            "cb_overlay_paper": health.get("cb_overlay_paper"),
            "research_track": health.get("research_track"),
            "regime_forward_paper": health.get("regime_forward_paper"),
        },
        "positions": None if positions is None else {
            "exported_at": positions.get("exported_at"),
            "n": len(positions.get("positions") or {}),
            "negative": list((positions.get("negative_positions") or {}).keys()),
        },
        "orders": None if orders is None else {
            "signal_date": orders.get("signal_date"),
            "count": orders.get("order_count"),
        },
    }
