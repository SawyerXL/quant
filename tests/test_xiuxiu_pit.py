"""下修标记 PIT 陷阱防护测试 (2026-10-01, 任务#20 框架件)。

测: ①未来事件不可见(2023 年下修在 2022 快照日必为 0——V2 未来信息 bug 的同型
防护) ②下修阈值(>5% 计下修, 分红级 1~3% 不计) ③多次下修封顶 2。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pandas as pd

from cb_xiuxiu_marker import pit_mark


def _events():
    # 110001: 2023-06 下修(−20%); 110002: 仅分红调整(−2%/−3%); 110003: 三次下修
    rows = [
        ("110001", "2023-06-01", 10.0, 8.0),
        ("110002", "2024-05-01", 10.0, 9.8),
        ("110002", "2025-05-01", 9.8, 9.5),
        ("110003", "2021-01-01", 10.0, 8.5),
        ("110003", "2022-01-01", 8.5, 7.0),
        ("110003", "2023-01-01", 7.0, 5.5),
    ]
    df = pd.DataFrame(rows, columns=["code", "change_date", "bef", "aft"])
    df["change_date"] = pd.to_datetime(df["change_date"])
    return df


def test_pit_guard():
    ev = _events()
    # 陷阱防护: 2023 的下修在 2022 年不可见
    assert pit_mark(ev, "110001", pd.Timestamp("2022-12-31")) == 0, \
        "未来下修事件泄漏进历史快照(PIT 陷阱失守)"
    assert pit_mark(ev, "110001", pd.Timestamp("2023-07-01")) == 1
    # 分红级调整不计下修(1.8%/3.1% 降幅 < 5% 阈值)
    assert pit_mark(ev, "110002", pd.Timestamp("2026-01-01")) == 0, \
        "分红调整被误判为下修(阈值过松)"
    # 多次下修封顶 2
    assert pit_mark(ev, "110003", pd.Timestamp("2023-06-01")) == 2
    print("PIT 陷阱防护 PASS: 未来事件不可见 / 分红不计 / 多次封顶")


if __name__ == "__main__":
    test_pit_guard()
