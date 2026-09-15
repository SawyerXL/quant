"""
Intent 层（2026-09-15 立项）——所有下单意图的单一入口。

背景（用户裁定）："约束写在实现里，不写在汇聚点"是本周故障的主线——
511360 豁免额未传导到 trader（09-14 假深熊清仓卖底仓）、688825 隔离
泄漏进恢复档，两起同型：豁免/隔离/白名单分散在各链路里各判一次。

结构：
  意图(来源, 标的, 方向, 数量, 约束) → 冲突检测 → 执行
  - 隔离清单在意图层判定（数据层隔离不传导到选股层的修复落点）
  - 豁免标的按属主判定：非属主源不得操作（sweep 拥有的 511360,
    trader 的熊市清仓不得卖——09-14 事故的直接语义）
  - 同标的近窗反向意图 = P0 冲突，拒绝执行

本周状态（用户裁定）：建层 + 隔离自测 + 冲突用例注入；
**下周才接管真实下单**——本模块暂不接入 trader/sweep 的实盘路径。
接入时须合并现有双写：cash_sweep.BOND_ETF 与 trader.BEAR_EXEMPT_CODES
统一为本注册表的豁免条目（intent 层验收清单第 1 条）。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# 豁免注册表：{code: 属主源}。属主=该持仓的管理者（sweep/cb/手动），
# 其他源对该标的的操作一律拒绝。单一来源原则：并入实盘路径后，
# cash_sweep.BOND_ETF / trader.BEAR_EXEMPT_CODES 删除，改读本表。
EXEMPT_REGISTRY: Dict[str, str] = {
    "511360": "sweep",   # 短债现金管理持仓（09-14 假深熊被 trader 卖出）
}

# 隔离注册表：数据层隔离必须传导到执行层（688825 泄漏教训）——
# 隔离标的任何来源都不得操作。
QUARANTINE_REGISTRY: Dict[str, str] = {
    "688825": "07-27 金额异常待外部源裁定（2026-09-13）",
}


@dataclass(frozen=True)
class Intent:
    source: str       # track_a / track_cb / sweep / manual
    code: str
    direction: str    # buy / sell
    qty: int
    price: float = 0.0
    ts: str = ""


@dataclass
class Decision:
    ok: bool
    reason: str = "ok"


class IntentRouter:
    """下单意图单一入口。冲突检测窗口内保留全部意图（含被拒的——
    被拒意图也是冲突证据）。"""

    def __init__(self, exempt: Dict[str, str] = None,
                 quarantine: Dict[str, str] = None):
        self.exempt = dict(exempt if exempt is not None else EXEMPT_REGISTRY)
        self.quarantine = dict(quarantine if quarantine is not None
                               else QUARANTINE_REGISTRY)
        self.window: List[Intent] = []
        self.refused: List[Tuple[Intent, str]] = []

    def submit(self, intent: Intent) -> Decision:
        # ① 隔离: 任何来源都不得操作隔离标的（数据层→执行层的传导点）
        if intent.code in self.quarantine:
            d = Decision(False, f"隔离标的: {self.quarantine[intent.code]}")
            self.refused.append((intent, d.reason))
            return d
        # ② 豁免: 标的属主之外的源不得操作（09-14 事故语义）
        owner = self.exempt.get(intent.code)
        if owner and owner != intent.source:
            d = Decision(False, f"豁免标的(属主 {owner}), 非属主源 {intent.source} 不得操作")
            self.refused.append((intent, d.reason))
            return d
        # ③ 反向冲突: 同标的近窗内存在相反方向意图（含被拒的）→ P0
        for past in self.window + [i for i, _ in self.refused]:
            if (past.code == intent.code
                    and past.direction != intent.direction
                    and past.source != intent.source):
                d = Decision(False, f"反向意图冲突: {past.source} {past.direction} "
                                    f"vs {intent.source} {intent.direction}")
                self.refused.append((intent, d.reason))
                return d
        self.window.append(intent)
        return Decision(True)

    def clear_window(self):
        self.window.clear()
        self.refused.clear()
