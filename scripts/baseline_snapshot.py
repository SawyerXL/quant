"""
Day-1 基线快照（2026-09-06 建）— 静跑期对账起点
用法: python scripts/baseline_snapshot.py [--out PATH] [--force]
9/7 bear 清仓后运行（一次性 cron 15:20）。快照内容: 持仓/净值/配置
（策略参数文件哈希 + git HEAD + 关键 settings）。存在即拒绝覆盖——
基线只应有一份, 两个月后所有对账以它为起点（用户 2026-09-06 定）。
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger

SNAP_FILE     = ROOT / "logs" / "qmt_positions_latest.json"
NAV_FILE      = ROOT / "logs" / "qmt_nav_history.parquet"
PARAMS_DIR    = ROOT / "config" / "strategy_params"
DEFAULT_OUT   = ROOT / "logs" / "static_period_baseline.json"
# Day-1 最早可行日(9/7 清仓日)。快照导出日期必须 ≥ 此日, 否则基线推迟——
# 用户 2026-09-06 定: 基线必须当日新鲜, 不接受旧快照凑合起算
DAY1_EARLIEST = "2026-09-07"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def build() -> dict:
    payload = {"generated_at": datetime.now().isoformat(),
               "day": 1, "purpose": "静跑期对账基线(9/7清仓后)"}

    # 1. 持仓快照(来自 Windows 15:10 导出 + Linux 15:12 qmt_snapshot 拉取;
    #    隧道断时该文件可能是旧快照——记录 exported_at 与新鲜度, 不静默)
    snap = {}
    if SNAP_FILE.exists():
        try:
            d = json.loads(SNAP_FILE.read_text(encoding="utf-8"))
            snap["exported_at"] = d.get("exported_at", "")
            snap["account"] = d.get("account", {})
            snap["positions"] = d.get("positions", {})
            snap["position_count"] = len(d.get("positions", {}))
            snap["fresh"] = str(d.get("exported_at", "")) >= DAY1_EARLIEST
            snap["note"] = ("account.total_assets 含仿真污染值(4888万), "
                            "真实净值以 qmt_nav_history 为准(见 qmt_live_nav_tracking 记忆)")
        except Exception as e:
            snap = {"error": str(e)}
    payload["snapshot"] = snap

    # 2. 净值(NAV 追踪口径)
    nav = {}
    if NAV_FILE.exists():
        try:
            import pandas as pd
            df = pd.read_parquet(NAV_FILE)
            nav = {"rows": int(len(df)),
                   "last": df.tail(1).to_dict(orient="records")[0]
                   if len(df) else {}}
        except Exception as e:
            nav = {"error": str(e)}
    payload["nav_history"] = nav

    # 3. 配置: git HEAD + 策略参数哈希 + 关键 settings
    import subprocess
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        payload["git_head"] = head.stdout.strip() if head.returncode == 0 else ""
    except Exception:
        payload["git_head"] = ""
    params = {}
    if PARAMS_DIR.exists():
        for f in sorted(PARAMS_DIR.glob("*.py")):
            if f.name.startswith("__"):
                continue
            try:
                params[f.name] = {"sha256": _sha256(f),
                                  "mtime": datetime.fromtimestamp(
                                      f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
            except Exception:
                pass
    payload["strategy_params"] = params
    from config import settings
    payload["key_settings"] = {
        k: getattr(settings, k, None) for k in
        ("DATA_SOURCE", "COMMISSION_RATE", "STAMP_TAX", "MAX_ACCOUNT_DRAWDOWN",
         "MAX_ORDER_AMOUNT", "IS_PROD")}
    return payload


def main():
    parser = argparse.ArgumentParser(description="Day-1 基线快照")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--force", action="store_true", help="覆盖已有基线/跳过新鲜度门槛(慎用)")
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"基线已存在({out})——正常。静跑期基线只应有一份, 此任务完成。")
        return 0

    payload = build()
    fresh = bool(payload["snapshot"].get("fresh"))
    if not fresh and not args.force:
        # 快照非当日新鲜(隧道断时是旧文件) → 基线推迟, Day 1 顺延至首个成功落盘日
        print(f"⏳ 基线推迟: 持仓快照导出于 {payload['snapshot'].get('exported_at', '?')}"
              f" (非 {DAY1_EARLIEST} 后新鲜数据)。"
              f"Day 1 顺延至首个新鲜快照日, 不接受旧快照凑合起算(§6.8 ⑧)。")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    logger.info(f"基线已写入: {out}")
    print(f"Day-1 基线: {out}")
    print(f"  git HEAD:    {payload['git_head'][:12]}")
    print(f"  持仓快照:    {payload['snapshot'].get('position_count', '?')} 只"
          f" (导出于 {payload['snapshot'].get('exported_at', '?')},"
          f" 新鲜={payload['snapshot'].get('fresh')})")
    print(f"  NAV 行数:    {payload['nav_history'].get('rows', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
