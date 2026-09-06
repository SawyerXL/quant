"""
短债现金管理（2026-09-04，队列#4 现金→短债的执行层升级）。

规则(简单稳健版):
  - 档位≤0.5: 闲置现金买入短融ETF(511360, tick 0.001, 几乎无回撤
    实测2019-2026最大回撤-1.1%/年化2.17%)
  - 档位>0.5: 全部ETF持仓卖回现金(股票买入需要现金时自动腾挪)
  - 仅在调仓日/档位切换日执行, 阈值2万以下不动(省摩擦)
回测量化(部署口径pool30×50万lot约束): +0.07pp年化, 回撤-0.26pp
——量级低于队列预估的0.2~0.4pp, 诚实标注; 熊市档现金占比越高增益越大。
用法: python scripts/cash_sweep.py [--dry-run]
挂接: run_daily.bat 在 fetch_and_execute 之后调用。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from monitoring.alerts import send_alert

BOND_ETF = "511360"
BOND_TICK = 0.001
MIN_AMOUNT = 20_000          # 2万以下不折腾
STOCK_CAPITAL = 1_000_000    # 方案A: 股票track名义100万


def _get_tier():
    """当前档位(读最近一组信号的position_ratio, 两组一致)。"""
    for g in ("g0", "g1"):
        p = Path(f"data_store/meta/signal_a_{g}.json")
        if p.exists():
            try:
                return float(json.loads(p.read_text(encoding="utf-8"))
                             .get("position_ratio", 1.0))
            except Exception:
                continue
    return 1.0


def main(dry_run: bool):
    from execution.qmt_client import get_client
    c = get_client()
    tier = _get_tier()
    pos = c.get_positions()
    bond_held = sum(v.get("market_value", 0)
                    for k, v in pos.items()
                    if str(k).split(".")[0] == BOND_ETF)
    bond_shares = sum(v.get("volume", 0)
                      for k, v in pos.items()
                      if str(k).split(".")[0] == BOND_ETF)
    # 股票市值(排除转债+债券ETF)
    cb_p = ("110", "111", "113", "118", "123", "127", "128")
    stock_mv = sum(v.get("market_value", 0) for k, v in pos.items()
                   if str(k).split(".")[0][:3] not in cb_p
                   and str(k).split(".")[0] != BOND_ETF)

    if tier > 0.5:
        target = 0.0                      # 高仓位: 现金全留
    else:
        target = STOCK_CAPITAL * (1 - tier)   # 档位现金升级为短债
    target = max(0.0, min(target, STOCK_CAPITAL * 0.7))

    delta = target - bond_held
    # 分批建仓(spec §7 2d: 单日新增敞口≤20万)——首次建仓50万分3天。
    # 例外(2026-09-06 定案): bear清仓日(tier≤0.3)现金一次性全进短债——
    # 分批规则防的是"新增风险敞口"突变, 现金→短债(波动0.7%)是低风险
    # 资产切换不适用; 且80万裸现金零收益空转5天无意义
    MAX_PER_RUN = 200_000
    if delta > MAX_PER_RUN and tier > 0.3:
        logger.info(f"[cash_sweep] 分批: 本次买入封顶{MAX_PER_RUN:,.0f} "
                    f"(总缺口{delta:,.0f})")
        delta = MAX_PER_RUN
    elif delta > MAX_PER_RUN and tier <= 0.3:
        logger.info(f"[cash_sweep] bear清仓日: 一次性配置{delta:,.0f} "
                    f"(豁免分批, 短债为低风险资产切换)")
    if abs(delta) < MIN_AMOUNT and not (tier > 0.5 and bond_held > 0):
        logger.info(f"[cash_sweep] tier={tier} 债券持仓{bond_held:,.0f} "
                    f"目标{target:,.0f} 差{delta:+,.0f} < {MIN_AMOUNT} 不动")
        return

    msg = f"[cash_sweep] tier={tier:.0%} 债券{bond_held:,.0f}→目标{target:,.0f}"
    if delta > 0:
        # 买入短融ETF: 参考价=持仓现价或昨收估算
        ref = bond_held / bond_shares if bond_shares else 113.9
        qty = int(delta / ref / 100) * 100   # ETF一手100份
        if qty <= 0:
            return
        if dry_run:
            logger.info(f"{msg} [DRY] 拟买{qty}份 @~{ref:.3f}")
            return
        oid = c.place_order(BOND_ETF, "buy", qty, ref * 1.002)
        logger.info(f"{msg} 买入{qty}份 → {oid}")
        send_alert(f"{msg}\n买入 {BOND_ETF} {qty}份")
    else:
        if dry_run:
            logger.info(f"{msg} [DRY] 拟卖{bond_shares}份")
            return
        ref = (bond_held / bond_shares if bond_shares else 113.9)
        oid = c.place_order(BOND_ETF, "sell", bond_shares, ref * 0.998)
        logger.info(f"{msg} 卖出{bond_shares}份 → {oid}")
        send_alert(f"{msg}\n卖出 {BOND_ETF} {bond_shares}份")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
