"""人工删笔清单(2026-10-14 实现, 十二/十三/十六轮锁定规格)。

目的: 不让链自动补回人工删掉的单——双向生效: 删掉的买单不自动补、删掉
的卖单(复核后决定暂不卖)不被链自动卖出。清单有效期至下个调仓日(目标
重算后残差语义恢复)。

强制退出优先于清单(十三轮): 清单内券触发强制退出(临期/强赎/评级)→
不再静默排除 → 告警 + 按"上锁期间强制退出"规则处理(>5交易日记账次日
再审 / ≤5交易日停下升级裁定——该规则为人工程序, 代码侧职责=不抑制+
告警留痕)。

文件格式 logs/manual_delete_list.json(与 manual_exec_lock.json 同目录
约定, Windows 链 CWD 相对路径):
  {"valid_until": "2026-10-29",
   "entries": [{"code": "127038", "direction": "buy",
                "reason": "事件复核删除", "deleted_at": "2026-10-19"}]}
缺失/过期 → 空清单, 不报错不告警(正常态)。
"""
import json
from datetime import date
from pathlib import Path

DELETE_LIST_PATH = Path("logs/manual_delete_list.json")


def _norm(c):
    return str(c).split(".")[0]


def load_list(path: Path | None = None) -> dict:
    """读清单; 文件缺失或过期(valid_until < 今天)→ 返回空。"""
    path = path or DELETE_LIST_PATH
    if not path.exists():
        return {"valid_until": "", "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"valid_until": "", "entries": []}
    until = str(data.get("valid_until", ""))
    if until and until < date.today().isoformat():
        return {"valid_until": until, "entries": []}
    return data


def suppression_sets(dl: dict, forced_exit_codes: set) -> tuple:
    """→ (suppress_sells, suppress_buys, overrides)。

    forced_exit_codes = 信号 excluded 的键(临期/强赎/评级摘除)。清单内券
    触发强制退出 → 进 overrides(不抑制+告警), 其余按方向进抑制集合。"""
    s_sells, s_buys, overrides = set(), set(), []
    for e in dl.get("entries", []):
        code = _norm(e.get("code", ""))
        if not code:
            continue
        d = e.get("direction")
        if code in forced_exit_codes:
            overrides.append({"code": code, "direction": d,
                              "reason": e.get("reason", "")})
            continue
        if d == "sell":
            s_sells.add(code)
        elif d == "buy":
            s_buys.add(code)
    return s_sells, s_buys, overrides


def generate_from_deletions(deletions: list, valid_until: str) -> dict:
    """从考试执行记录的删笔条目生成清单(十三轮: 不手工录入=113647 教训)。

    deletions = [{"code": "127038", "direction": "buy", "reason": "..."}]
    → 每条补 deleted_at=今天。"""
    entries = []
    for d in deletions:
        code = _norm(d.get("code", ""))
        direction = d.get("direction")
        reason = d.get("reason", "")
        if not code or direction not in ("buy", "sell"):
            raise ValueError(f"非法删笔条目: {d}")
        entries.append({"code": code, "direction": direction,
                        "reason": reason,
                        "deleted_at": date.today().isoformat()})
    return {"valid_until": valid_until, "entries": entries}
