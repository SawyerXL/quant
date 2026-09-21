"""
单变量增量 harness（2026-09-14 凌晨落成）——四个模型改进方向的公共
基础设施，方向切换 = logs/single_var_directions.json 里一行配置，
禁止复制脚本。

设计（与 logs/single_var_registration_20260914.md 绑定）：
- 基准臂固定：800等权+现金（pit800 + 五档择时现网冻结 + 组件全关），
  四个方向共用同一基准臂，一次运行只算一遍。
- 每个方向 = 唯一变量 + 唯一参数值（禁网格——登记文档 §3a）。
- TE/SE/σ/MDE 由 te_stats() 唯一实现（锁定方法，登记文档 §1）：
  TE=月频非重叠对数差分样本σ×√12，全期不收缩，SE=TE/√T。
  σ 只报点值，不确定性报效应±SE（登记文档 §2）。
- 注册未完成(registered=false)或参数值未锁定(value_locked=false)的
  方向 fail-fast 拒跑（闸门五：配置缺失禁 fallback）。
- --validate 只验配置/注册状态/字段合法性，不跑任何回测数字。

用法:
  python scripts/single_var_harness.py --validate
  python scripts/single_var_harness.py --direction D1_timing_reestimate \
      [--paths 10] [--cost 0.0013]
输出: 各臂曲线 logs/sv_curve_{id}_{arm}.csv + 汇总 logs/sv_summary.csv
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import (WINDOWS, A0_OV, load_blacklist,
                                         load_pit_memberships, load_entry_map,
                                         members_on)

REG_PATH = Path(__file__).parent.parent / "logs" / "single_var_directions.json"


def te_stats(ens_base, ens_test, years):
    """TE 估计锁定方法唯一实现（登记文档 §1，改动即违反方法锁）：
    TE = 月频非重叠对数收益差分序列的样本标准差 × √12；
    全期估计，不做收缩，不做自相关校正；SE = TE / √T，T = 实际年数。"""
    d = (np.log(ens_test) - np.log(ens_base)).dropna()
    m = d.resample("ME").last().dropna()
    if len(m) < 24:
        raise ValueError(f"月频差分样本 n={len(m)} < 24, 不支持统计判读")
    te = float(m.std(ddof=1)) * np.sqrt(12.0)
    return {"TE_pct": te * 100.0, "SE_pp": te / np.sqrt(years) * 100.0,
            "n_months": int(len(m)), "years": years}


def build_panels(start, end):
    """与 s1_attribution_clean.build_panels 同口径: 黑名单排除、
    价格断裂日 close→NaN(引擎 NaN 语义=该日零贡献)、面板∩交易日历。"""
    load_start = (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, load_start, end)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            if "price_break_flag" in d.columns:
                d.loc[d["price_break_flag"] == 1, "close"] = float("nan")
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
                if len(amt) >= 200:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    sh = load_daily("SH000001", load_start, end)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, ap, cal


def run_arm(panel, ap, ic, dates, cfg, pit_map, start_ts, use_timing):
    """单臂: 10路径摊平。返回 (ens曲线, 路径年化均值, 换手/年)。"""
    anns, rets, tos = [], [], []
    years = 1.0
    for dts in dates:
        nav, info = run_backtest(panel, ap, dts, cfg,
                                 ic if use_timing else None,
                                 pit_members=pit_map)
        nav = nav[nav.index >= start_ts]
        cm = calc_metrics(nav)
        anns.append(float(cm["年化_float"]))
        rets.append(nav.pct_change().dropna())
        years = (nav.index[-1] - nav.index[0]).days / 365.25
        tos.append(info["total_commission"] / cfg.commission / years)
    j = pd.concat(rets, axis=1).dropna()
    ens = (1 + j.mean(axis=1)).cumprod()
    return ens, float(np.mean(anns)), float(np.mean(tos))


def load_registry():
    if not REG_PATH.exists():
        raise SystemExit(f"方向注册表缺失: {REG_PATH}（闸门五: 禁 fallback）")
    reg = json.loads(REG_PATH.read_text(encoding="utf-8"))
    for d in reg.get("directions", []):
        for field in ("id", "name", "mode", "registered", "value_locked"):
            if field not in d:
                raise SystemExit(f"注册表 {d.get('id', '?')} 缺字段 {field}（闸门五）")
    return reg


def validate():
    """只验配置与注册状态, 不跑数。override 键必须是 BacktestConfig 字段
    （防拼错参数静默变 no-op——与闸门五同型）。"""
    reg = load_registry()
    valid_keys = set(BacktestConfig().__dict__.keys()) | {"timing_off"}
    base_desc = reg["base_arm"]["desc"]
    print(f"基准臂: {reg['base_arm']['name']} — {base_desc}")
    print(f"TE 方法: {reg['te_method']}")
    for d in reg["directions"]:
        if d["mode"] != "backtest":
            print(f"  {d['id']} [{d['mode']}] {d['name']}: 不跑"
                  f"（{d.get('note', '')[:40]}）")
            continue
        blockers = []
        if not d["registered"]:
            blockers.append("未事前登记")
        if not d["value_locked"]:
            blockers.append("参数值未锁定")
        bad = [k for k in d.get("override", {}) if k not in valid_keys]
        if bad:
            blockers.append(f"override 含非法字段 {bad}")
        if d.get("effect_sign") not in (1, -1):
            blockers.append("effect_sign 非法")
        pp = d.get("plausible_effect_pp")
        if not isinstance(pp, list) or len(pp) != 2:
            blockers.append("plausible_effect_pp 缺失/格式错")
        state = "可跑" if not blockers else f"拒跑({'; '.join(blockers)})"
        print(f"  {d['id']} [{d['mode']}] {d['name']}: {state}")
    return 0


def run_direction(dir_id, paths=10, cost=0.0013):
    reg = load_registry()
    d = next((x for x in reg["directions"] if x["id"] == dir_id), None)
    if d is None:
        raise SystemExit(f"方向 {dir_id} 不在注册表（闸门五: 禁 fallback）")
    if d["mode"] != "backtest":
        raise SystemExit(f"{dir_id} mode={d['mode']} 不可跑（{d.get('note','')}）")
    if not d["registered"]:
        raise SystemExit(f"{dir_id} 未事前登记, 拒跑（登记文档 §5: 看过结果无登记资格）")
    if not d["value_locked"]:
        raise SystemExit(f"{dir_id} 参数值未锁定, 拒跑（登记文档 §3a）")

    START, END = WINDOWS["main"]
    panel, ap, cal = build_panels(START, END)
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    base_dates = [x for x in make_rebal_dates(cal, "biweekly")
                  if START <= x <= END]
    idx = {x: i for i, x in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(x, 0) + off] for x in base_dates
                   if idx.get(x, 0) + off < len(cal)]
        return [x for x in shifted if START <= x <= END]

    date_paths = [path(off) for off in range(paths)]
    all_dates = sorted({x for ps in date_paths for x in ps})
    pit = load_pit_memberships()
    entry_map = load_entry_map()
    pit_map = {x: sorted(members_on(x, pit, entry_map) or [])
               for x in all_dates}
    start_ts = pd.Timestamp(START)
    years = (pd.Timestamp(END) - start_ts).days / 365.25

    base_cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **A0_OV,
                                 "commission": cost})
    ens_base, base_ann, base_turn = run_arm(panel, ap, ic, date_paths,
                                            base_cfg, pit_map, start_ts, True)
    test_cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **A0_OV,
                                 **d.get("override", {}), "commission": cost})
    use_timing = not d.get("timing_off", False)
    ens_test, test_ann, test_turn = run_arm(panel, ap, ic, date_paths,
                                            test_cfg, pit_map, start_ts,
                                            use_timing)

    ts = te_stats(ens_base, ens_test, years)
    effect_raw_pp = (test_ann - base_ann) * 100.0
    effect_pp = effect_raw_pp * d["effect_sign"]   # 变量贡献(正=有利)
    se_pp = ts["SE_pp"]
    sigma = effect_pp / se_pp
    mde_pp = 0.35 * se_pp
    lo, hi = d["plausible_effect_pp"]
    # MDE 后验闸门(登记文档§3b/§6 D2, 2026-09-13晚): 实测 MDE > 可信效应
    # 上沿 = 用错口径的登记无效, σ 过线也不给候选资格
    mde_ok = mde_pp <= hi
    if sigma < 0.35:
        verdict = "不通过"
    elif not mde_ok:
        verdict = "MDE不满足(实测MDE>可信效应上沿, 登记口径无效)"
    else:
        verdict = "候选资格(非确认)"
    ci = f"[{effect_pp - 1.96*se_pp:+.2f}, {effect_pp + 1.96*se_pp:+.2f}]pp"

    print(f"\n方向 {d['id']} {d['name']} — 变量: {d['variable']} = {d['value']}")
    print(f"基准臂 路径均值{base_ann*100:+.2f}% 换手{base_turn*100:.0f}%/年 | "
          f"test臂 路径均值{test_ann*100:+.2f}% 换手{test_turn*100:.0f}%/年")
    print(f"效应(变量贡献) {effect_pp:+.2f}pp, 95%CI {ci}（效应±1.96SE, σ不报区间）")
    print(f"TE={ts['TE_pct']:.1f}% (n={ts['n_months']}月) SE={se_pp:.2f}pp "
          f"σ={sigma:+.2f}(点值) MDE(0.35σ)={mde_pp:.2f}pp → {verdict}")
    print(f"登记效应量 {lo}~{hi}pp | 实测MDE(0.35×实测SE)={mde_pp:.2f}pp"
          f"（事前MDE见登记文档§4） | 预期假阳率提醒: 单边p(0.35σ)≈0.36, "
          f"通过≠确认")

    ens_base.to_csv(f"logs/sv_curve_{d['id']}_BASE.csv")
    ens_test.to_csv(f"logs/sv_curve_{d['id']}_TEST.csv")
    row = {"direction": d["id"], "variable": d["variable"], "value": d["value"],
           "return_basis": "price", "benchmark_basis": "price",
           "base_ann": round(base_ann, 6), "test_ann": round(test_ann, 6),
           "effect_pp": round(effect_pp, 4), "TE_pct": round(ts["TE_pct"], 2),
           "SE_pp": round(se_pp, 4), "sigma": round(sigma, 4),
           "MDE_pp": round(mde_pp, 4), "n_months": ts["n_months"],
           "verdict": verdict}
    pd.DataFrame([row]).to_csv("logs/sv_summary.csv", index=False,
                               encoding="utf-8-sig")
    print(f"落盘: logs/sv_curve_{d['id']}_*.csv, logs/sv_summary.csv")
    return 0


def _write_state(payload):
    """运行状态心跳(2026-09-13 晚): 供健康行 sv_harness 字段——正常运行
    路径也要有心跳, 否则失败与"从未跑过"无法区分。自身不抛。"""
    try:
        p = Path(__file__).parent.parent / "logs" / "single_var_harness_state.json"
        p.write_text(json.dumps({**payload,
                                 "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def main():
    import argparse
    ap_ = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap_.add_argument("--validate", action="store_true", help="只验配置不跑数")
    ap_.add_argument("--direction", default=None, help="方向id, 如 D1_timing_reestimate")
    ap_.add_argument("--paths", type=int, default=10)
    ap_.add_argument("--cost", type=float, default=0.0013)
    args = ap_.parse_args()
    if args.validate:
        _write_state({"status": "ok", "action": "validate"})
        return validate()
    if args.direction:
        try:
            _write_state({"status": "running", "direction": args.direction})
            rc = run_direction(args.direction, args.paths, args.cost)
            _write_state({"status": "ok", "direction": args.direction})
            return rc
        except SystemExit as e:
            # fail-fast 拒跑是预期行为(非静默失败), 状态=refused 不触发告警
            _write_state({"status": "refused", "direction": args.direction,
                          "error": str(e)})
            raise
        except Exception as e:
            _write_state({"status": "error", "direction": args.direction,
                          "error": repr(e)})
            raise
    raise SystemExit("需要 --validate 或 --direction（今晚禁跑数, 用 --validate）")


if __name__ == "__main__":
    sys.exit(main())
