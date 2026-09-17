"""09-22 CB 裁定 ρ 计算器（判据锁于 2026-09-15 出数前，账本 1106 行）。

Spearman ρ = 重算信号 vs 旧信号(2026-09-04, 冻结件) 的排序相关。
锁死判读矩阵：ρ≥0.9 续冻或补齐 / 0.7~0.9 不得据旧信号加仓 /
ρ<0.7 倾向平仓（不得再找新理由续冻）。
链未恢复分支：延期已用一次（09-15→09-22），二次按 ρ<0.7 处理。
"""
import json
import sys
from pathlib import Path

from loguru import logger

OLD = Path("logs/cb_signal_20260904_frozen.json")
NEW = Path("data_store/meta/signal_cb_latest.json")
EXPIRY = "2026-09-22"


def rank_positions(holdings):
    return {code: i for i, code in enumerate(holdings)}


def spearman_rho(old_ranks, new_ranks):
    """对共同标的的位次向量算 Spearman ρ（纯实现，不用 scipy 免依赖）。"""
    codes = [c for c in old_ranks if c in new_ranks]
    if len(codes) < 2:
        return codes, float("nan")
    import statistics
    x = [old_ranks[c] for c in codes]
    y = [new_ranks[c] for c in codes]
    mx, my = statistics.mean(x), statistics.mean(y)
    sx = statistics.pstdev(x)
    sy = statistics.pstdev(y)
    if sx == 0 or sy == 0:
        return codes, float("nan")
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / len(codes)
    return codes, cov / (sx * sy)


def verdict(rho):
    if rho >= 0.9:
        return "续冻或按新信号补齐（持有依据成立）"
    if rho >= 0.7:
        return "旧信号仅供参考，不得据旧信号加仓"
    return "倾向平仓（旧信号不可用，B 冻结依据消失）"


SNAPSHOT = Path("logs/qmt_positions_latest.json")


def _close_list_from_snapshot(signal):
    """平仓张数从当日快照取实际 volume；信号份额只旁注。

    新鲜度闸门：exported_at 必须=今日且 ≥15:00（收盘后采信纪律，
    回测复验#5）。不新鲜 → 不出清单只出代码，防止 15:05 与快照拉取
    撞车时按旧文件下数量。"""
    from datetime import datetime
    frozen = json.loads(Path("logs/cb_holdings_frozen.json")
                        .read_text(encoding="utf-8"))["codes"]
    if not SNAPSHOT.exists():
        logger.error("快照文件缺失 → 不出清单（仅代码列表，张数待快照）")
        for code in frozen:
            logger.info(f"  待平仓 {code}（数量待快照确认）")
        return 2
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    exported = snap.get("exported_at", "")
    try:
        ts = datetime.fromisoformat(exported)
    except (TypeError, ValueError):
        ts = None
    today = datetime.now()
    fresh = (ts is not None and ts.date() == today.date()
             and ts.hour >= 15)
    if not fresh:
        logger.error(f"快照不新鲜（exported_at={exported}）→ 不出清单，"
                     "只出代码+张数待快照确认（须检查 15:05 拉取是否落地）")
        for code in frozen:
            logger.info(f"  待平仓 {code}（数量待新鲜快照）")
        return 2
    positions = snap.get("positions", {})
    sig_shares = signal.get("shares", {})
    logger.info(f"快照 exported_at={exported} 新鲜 ✓ 平仓清单：")
    mismatch = 0
    for code in frozen:
        vol = positions.get(code, {}).get("volume")
        sig = sig_shares.get(code)
        if vol is None:
            logger.warning(f"  {code}: 快照无持仓 → 数量按 0（或快照键位查）")
            mismatch += 1
            continue
        mark = ""
        if sig is not None and vol != sig:
            mark = f" ⚠ 与信号份额({sig})背离→以快照为准,记FWD-002"
            mismatch += 1
        logger.info(f"  卖出 {code} × {vol} 张{mark}")
    if mismatch:
        logger.info(f"背离 {mismatch}/6 只：份额背离记 FWD-002 归因表"
                    "第五类'上游停摆'条目。")
    return 2


def main():
    old = json.loads(Path(OLD).read_text(encoding="utf-8"))
    if not Path(NEW).exists():
        logger.error(f"新信号文件缺失: {NEW}")
        sys.exit(1)
    new = json.loads(Path(NEW).read_text(encoding="utf-8"))
    old_hold = old["holdings"]
    new_hold = new["holdings"]
    new_date = new.get("signal_date")
    logger.info(f"旧信号: {old.get('signal_date')} ({len(old_hold)}只) | "
                f"新信号: {new_date} ({len(new_hold)}只)")
    logger.info(f"到期日: {EXPIRY} | 冻结六只: "
                f"{json.loads(Path('logs/cb_holdings_frozen.json').read_text(encoding='utf-8'))['codes']}")

    if new_date == old.get("signal_date"):
        logger.warning("新信号日期=旧信号 2026-09-04 → CB 链未恢复。"
                       "按锁死规则：一次延期已于 09-15 消耗（冻结至 09-22 带"
                       "到期日）；今日是第二次延期请求 → **不允许**，按 ρ<0.7 "
                       "处理 → 默认倾向平仓，交人工裁定。任何续冻决定必须写"
                       "新到期日+书面理由并记入账本（不是跳过/顺延）。"
                       "ρ 不计算（无新信号）。")
        logger.info("默认动作=平仓清单。张数来源=当日新鲜快照（信号份额仅"
                    "旁注对照，不作下单依据；两者背离时以快照为准并记 "
                    "FWD-002）。")
        sys.exit(_close_list_from_snapshot(new))

    common, rho = spearman_rho(rank_positions(old_hold), rank_positions(new_hold))
    jac = len(common) / len(set(old_hold) | set(new_hold))
    logger.info(f"共同标的 {len(common)}/25 | Jaccard {jac:.3f} | ρ = {rho:+.4f}")
    # 残骸规则（2026-09-16 深夜锁）：先读实现度再读 ρ——持仓是"建仓
    # 25 笔仅成交 6 + 09-03 补买腿废单"的残骸，24% 实现度本身构成
    # 平仓理由，与信号相关性无关；ρ 矩阵仅在实现度过关时适用
    if SNAPSHOT.exists():
        try:
            acct = set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))
                       .get("positions", {}).keys())
        except Exception:
            acct = set()
        impl = len([c for c in new_hold if c in acct]) / len(new_hold)
        logger.info(f"实现度 = 账户持有 {len([c for c in new_hold if c in acct])}"
                    f"/{len(new_hold)} = {impl:.0%}")
        if impl < 1.0:
            logger.info("判读: 实现度不过关 → 平仓（残骸规则，与 ρ 无关）。"
                        "ρ 仅作参考: " + verdict(rho))
            sys.exit(_close_list_from_snapshot(new))
    logger.info(f"判读: {verdict(rho)}")
    # 成员差异补充信息（ρ 只看共同标的，名单漂移本身也是信息）
    added = [c for c in new_hold if c not in old_hold]
    removed = [c for c in old_hold if c not in new_hold]
    if added or removed:
        logger.info(f"名单漂移: 新增 {added} / 移除 {removed}")


if __name__ == "__main__":
    main()
