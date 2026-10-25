"""CB 执行结果汇总（2026-10-25）。

读取 logs/execution_result_*_cb.json（10-09 换血、10-12 非调仓等），
输出日期/目标/成交/填单率/平均与最大滑点。11-02 首次月度后重跑即可
得到执行差基线；不改执行链、不下单。

注意 schema 两版：
- 10-09: target_sell/filled_sell（订单数/股数混用，见原始 note）
- 10-12: target_buy_count/fill_count（订单数）
只做只读汇总，单位按字段名标注。
"""
import glob
import json
from datetime import datetime
from pathlib import Path

from loguru import logger


def main():
    rows = []
    for p in sorted(glob.glob(str(Path("logs") / "execution_result_*_cb.json"))):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            rows.append({"file": Path(p).name, "error": str(e)[:80]})
            continue
        rows.append({
            "file": Path(p).name,
            "date": d.get("signal_date"),
            "note": d.get("note", "")[:60],
            "target_buy": d.get("target_buy", d.get("target_buy_count")),
            "filled_buy": d.get("filled_buy", d.get("fill_count")),
            "target_sell": d.get("target_sell"),
            "filled_sell": d.get("filled_sell"),
            "fill_rate_pct": d.get("fill_rate_pct"),
            "avg_slip_pct": d.get("avg_slippage_pct"),
            "max_slip_pct": d.get("max_slippage_pct"),
            "blocked": d.get("blocked"),
            "rejected": d.get("skipped_rejected"),
        })
    for r in rows:
        logger.info(json.dumps(r, ensure_ascii=False))
    out = Path("logs") / "cb_exec_summary_20261025.json"
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "rows": rows}, ensure_ascii=False, indent=2))
    logger.info(f"-> {out}")


if __name__ == "__main__":
    main()
