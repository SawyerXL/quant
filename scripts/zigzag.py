"""ZigZag 转折点(2026-10-19 立项 R2, 第一阶段=实现+描述性)。

**前视纪律(本模块的核心约束)**: pivot(波峰/波谷)只有在价格自该极值反向
超过阈值后才被确认。返回的每个 pivot 同时带 `pivot_idx`(极值位置) 与
`confirm_idx`(确认位置)。**任何下游用途必须以 confirm_idx 为可用时点**,
禁止在 pivot_idx 当天使用该信息(否则=未来函数, 闸门二同族)。

阈值固定百分比(默认 5%, 不网格)。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Pivot:
    kind: str          # "high" | "low"
    pivot_idx: int     # 极值所在 bar
    price: float
    confirm_idx: int   # 反向超阈值、该 pivot 被确认的 bar

    @property
    def lag(self) -> int:
        return self.confirm_idx - self.pivot_idx


def zigzag_pivots(prices, threshold: float = 0.05) -> list[Pivot]:
    """prices: list[float] 或 pandas Series(按时间升序)。

    返回**已确认**的 pivot 序列; 序列末端的未确认极值不返回。
    """
    p = [float(x) for x in prices]
    n = len(p)
    if n < 2 or threshold <= 0:
        return []

    pivots: list[Pivot] = []
    direction = None          # None | "up" | "down"
    anchor_idx, anchor_px = 0, p[0]      # 起点(首个反向确认时成为 pivot)
    ext_idx, ext_px = 0, p[0]            # 当前方向上的极值

    for i in range(1, n):
        cur = p[i]
        if direction is None:
            if cur >= anchor_px * (1 + threshold):
                # 起点确认为低点(自此上涨超阈值)
                pivots.append(Pivot("low", anchor_idx, anchor_px, i))
                direction, ext_idx, ext_px = "up", i, cur
            elif cur <= anchor_px * (1 - threshold):
                # 起点确认为高点
                pivots.append(Pivot("high", anchor_idx, anchor_px, i))
                direction, ext_idx, ext_px = "down", i, cur
        elif direction == "up":
            if cur > ext_px:
                ext_idx, ext_px = i, cur
            elif cur <= ext_px * (1 - threshold):
                pivots.append(Pivot("high", ext_idx, ext_px, i))
                direction, ext_idx, ext_px = "down", i, cur
        else:  # down
            if cur < ext_px:
                ext_idx, ext_px = i, cur
            elif cur >= ext_px * (1 + threshold):
                pivots.append(Pivot("low", ext_idx, ext_px, i))
                direction, ext_idx, ext_px = "up", i, cur
    return pivots
