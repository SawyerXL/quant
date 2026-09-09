"""
单位 ground truth 锚采集 (2026-09-09 用户定, 归一的前置条件) —
从 MCP 独立源取 5 只票 × 3 日期的成交额/成交量真值, 落盘
tests/unit_ground_truth.json。锚日期选择: 2026-05-15(万元时代)、
2026-06-15(切换期)、2026-07-15(元时代)——钉死时间分层切换点。
MCP 返回字段显式标注: 成交量(万股)、成交额(万元)。
所有单位归一必须能复现本文件数值, 否则不允许 commit。
用法: python scripts/build_unit_ground_truth.py
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
logger.remove()

from data.source.mcp_source import MCPSource

STOCKS = ["600519", "000063", "300750", "000002", "600111"]
DATES = ["2026-05-15", "2026-06-15", "2026-07-15"]
OUT = Path(__file__).parent.parent / "tests" / "unit_ground_truth.json"


def main():
    src = MCPSource()
    anchor = {"source": "MCP FinQuery", "units_note":
              "成交额(万元)/成交量(万股) per MCP query spec",
              "collected_at": "2026-09-09", "stocks": {}}
    for code in STOCKS:
        anchor["stocks"][code] = {}
        for d in DATES:
            try:
                df = src.get_daily(code, d, d)
                if df.empty:
                    print(f"{code} {d}: MCP 空")
                    continue
                row = df.iloc[-1]
                anchor["stocks"][code][d] = {
                    "close": float(row.get("close", 0) or 0),
                    "amount": float(row.get("amount", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                }
                print(f"{code} {d}: close={anchor['stocks'][code][d]['close']} "
                      f"amount(万元)={anchor['stocks'][code][d]['amount']}")
            except Exception as e:
                print(f"{code} {d}: 失败 {e}")
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(anchor, f, ensure_ascii=False, indent=2)
    print(f"\n落盘: {OUT}")


if __name__ == "__main__":
    main()
