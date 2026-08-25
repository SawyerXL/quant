"""Snapshot store — read/write pre-computed JSON snapshots in data_store/web_cache/."""
import json
from datetime import datetime, date
from pathlib import Path
from web.config import SNAPSHOT_DIR


def _path(name: str, d: str | None = None) -> Path:
    d = d or str(date.today())
    return SNAPSHOT_DIR / f"{name}_{d}.json"


def write_snapshot(name: str, payload: dict, source: str = "", snapshot_date: str | None = None):
    """Write a named snapshot with metadata. Atomic write via temp file.

    Args:
        snapshot_date: Date string YYYY-MM-DD for the file name. Defaults to today.
    """
    snap = {
        "computed_at": datetime.now().isoformat(),
        "source": source,
        "payload": payload,
    }
    content = json.dumps(snap, ensure_ascii=False, indent=2)
    d = snapshot_date or str(date.today())
    p = _path(name, d)
    # Atomic write: temp file + rename
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(p)
    if d == str(date.today()):
        latest = SNAPSHOT_DIR / f"{name}_latest.json"
        tmp_latest = latest.with_suffix(".tmp")
        tmp_latest.write_text(content)
        tmp_latest.rename(latest)


def read_snapshot(name: str, d: str | None = None) -> dict | None:
    """Read latest snapshot for a name. Tolerant to corrupted files."""
    latest = SNAPSHOT_DIR / f"{name}_latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text())
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupted, try dated fallback
    dated = sorted(SNAPSHOT_DIR.glob(f"{name}_*.json"), reverse=True)
    for p in dated:
        if "_latest" in p.name:
            continue
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, ValueError):
            continue  # Skip corrupted files
    return None


def list_snapshots(name: str, limit: int = 30) -> list[dict]:
    """Return recent dated snapshots for history."""
    results = []
    for p in sorted(SNAPSHOT_DIR.glob(f"{name}_*.json"), reverse=True):
        if "_latest" in p.name:
            continue
        results.append(json.loads(p.read_text()))
        if len(results) >= limit:
            break
    return results
