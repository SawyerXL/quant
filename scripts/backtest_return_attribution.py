"""
收益归因（§6.9，2026-09-07 立项，待办#12 提至 P1）— 超额收益四段分解。

四臂:
  A0  800等权 buy&hold（T3 2026-09-08: 同引擎退化臂 pool_style=pit800,
      资金2000万使lot可行, 择时/MA10/TP/拥挤度/过热/止损全关;
      --a0-legacy 可追加旧独立实现对比）
  A1  pool30 等权 buy&hold（择时关: index_close=None → 引擎恒满仓）
  A2  pool30 + 五档择时（= §6.8 B最小栈）
  A3  完整栈（= §6.8 A现网）
分解: 池(选股)贡献 = A1−A0; 择时贡献 = A2−A1; 组件贡献 = A3−A2。

口径与 §6.8 四臂消融完全一致（biweekly × 10路径摊平, pool30×50万lot×
降档3%, 成本13bp/边）——A2/A3 应与 §6.8 数字复现, 内置一致性检查。
窗口: 主 2019-01-01~2026-08-28 + OOS 2015-01-01~2018-12-31。
已知口径限制（入档, 判读时带上）:
  ① A0 成员: 主窗口用 csi800_universe_bs 的 14 个 PIT 快照(前向填充);
     OOS 无快照, 用 csi800_with_history.entry_date 近似——退市/剔除成员
     缺失 → OOS 的 A0 有幸存者偏差(基准偏乐观, 判读方向保守)
  ② 全 panel 共享本地库幸存者样本问题(2019 起退市股缺失 ~0.3%),
     A1/A2/A3 同受, 对"差"的污染远小于对"水平"的污染
用法: python scripts/backtest_return_attribution.py [--window main|oos|both]
"""
import sys
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

WINDOWS = {
    "main": ("2019-01-01", "2026-08-28"),
    "oos":  ("2015-01-01", "2018-12-31"),
}
BASE = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}
# A0 退化臂(T3, 2026-09-08): 同引擎同路径, 池=PIT800等权, 择时/MA10/TP/
# 拥挤度/过热/止损全关。资金2000万: 50万下 800等权×100股 lot 全部买不起
# (625元/票)→全现金退化, 2000万(2.5万/票)仅跳过>250元股票(茅台类极少数),
# 与旧独立A0的小数权重语义贴近——资本口径差异在 a0_alignment_check 标红
A0_OV = {"pool_style": "pit800", "pool_size": 800,
         "initial_capital": 20_000_000.0,
         "enable_ma10_exit": False, "enable_take_profit": False,
         "enable_stops": False, "max_vol20": 999.0,
         "max_20d_return": 99999.0, "max_consec_up_days": 999,
         "max_5d_return": 99999.0}
ARMS = [
    ("A0_800等权buyhold",  "no_timing"),     # 引擎退化臂(pit800)
    ("A1_pool30买持",       "no_timing"),    # index_close=None
    ("A2_pool30+五档",      {"max_vol20": 999.0, "enable_ma10_exit": False,
                            "enable_take_profit": False}),   # = B最小栈
    ("A3_完整栈",           {}),             # = A现网
]


def load_blacklist() -> set:
    """qfq跳变未修复/新浪无覆盖/北交所真实大波动的代码, 回测池须排除
    (data_store/meta/price_blacklist.json, 2026-09-07 第三级审计产出)。"""
    import json
    p = Path("data_store/meta/price_blacklist.json")
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")).get("codes", []))
        except Exception:
            pass
    return set()


def load_pit_memberships() -> list:
    """csi800_universe_bs → [(snap_date, set(codes))] 按日期升序。"""
    df = pd.read_parquet("data_store/meta/csi800_universe_bs.parquet")
    out = []
    for _, r in df.iterrows():
        out.append((pd.Timestamp(r["date"]),
                    set(str(c) for c in r["codes"].split(","))))
    return sorted(out, key=lambda x: x[0])


def load_entry_map() -> dict:
    """csi800_with_history → {code: entry_date}（OOS 近似成员判定用）。"""
    df = pd.read_parquet("data_store/meta/csi800_with_history.parquet")
    return {str(r["code"]): pd.Timestamp(r["entry_date"])
            for _, r in df.iterrows()}


def members_on(date, pit, entry_map):
    """date 日的 800 成员。主窗口用 PIT 快照前向填充, 首快照(2019-06-28)
    之前回填首快照成员(避免 2019 上半年空仓低估 A0); OOS 用 entry_date
    (幸存者偏差: 剔除/退市成员缺失 → A0 OOS 偏乐观, 判读时保守)。"""
    date = pd.Timestamp(date)
    if pit:
        use = [s for s in pit if s[0] <= date]
        if use:
            return use[-1][1]
        return pit[0][1]  # 首快照回填
    return {c for c, e in entry_map.items() if e <= date} or None


def run_a0(panel, cal, rebal_paths, pit, entry_map):
    """A0: 800等权 buy&hold。逐日权重漂移 + 调仓日重置等权, 成本按换手扣。
    成员只在调仓日更新(与引擎"池在调仓日换"语义一致)。"""
    commission = DEFAULT_CONFIG.commission
    rets = panel.pct_change().fillna(0.0)
    path_anns, path_ret_frames, turns = [], [], []
    for rebal_dates in rebal_paths:
        rebal_set = set(pd.Timestamp(d) for d in rebal_dates)
        nav, w, cum_turn = 1.0, None, 0.0
        port_rets = []
        for t in panel.index:
            if t in rebal_set or w is None:
                mem = members_on(t, pit, entry_map)
                col = [c for c in mem if c in rets.columns] if mem else []
                if len(col) < 100:
                    continue  # 成员数据不足, 该日沿用旧权重(或空仓)
                w_new = pd.Series(1.0 / len(col), index=col)
                if w is not None:
                    aligned = w.reindex(col).fillna(0.0)
                    turn = float((w_new - aligned).clip(lower=0).sum())
                    cum_turn += turn
                    nav *= (1 - turn * commission)  # 调仓成本
                w = w_new
            r_t = rets.loc[t, w.index]
            port_ret = float((w * r_t).sum())
            nav *= (1 + port_ret)
            w = w * (1 + r_t) / (1 + port_ret)
            port_rets.append(port_ret)
        path_anns.append((nav ** (252 / len(panel.index)) - 1) if nav > 0 else -1)
        path_ret_frames.append(pd.Series(port_rets, index=panel.index[:len(port_rets)]))
        turns.append(cum_turn)
    j = pd.concat(path_ret_frames, axis=1).dropna()
    ens = (1 + j.mean(axis=1)).cumprod()
    return path_anns, turns, calc_metrics(ens)


def run_engine_arms(panel, ap, ic, cal, rebal_paths, years, window_start,
                    pit_map=None):
    """A0/A1/A2/A3 共用引擎; A0/A1 传 index_close=None 关择时。
    A0(T3): 同引擎退化臂 pool_style=pit800, pit_map={date: [codes]}。

    window_start: 回测窗口起点——面板已预加载(前推250+交易日暖机),
    指标从窗口首日即有值且只用窗口前历史(无前视); 净值/摊平指标按
    窗口段切片计算, 预加载段(近似现金收益)不计入。
    """
    results = {}
    ws = pd.Timestamp(window_start)
    for name, ov in ARMS:
        if ov is None:
            continue
        is_a0 = name.startswith("A0")
        anns, rets, tos = [], [], []
        for off, dates in enumerate(rebal_paths):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    **({} if ov == "no_timing" else ov)})
            if is_a0:
                cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                        **A0_OV})
            ic_arg = None if ov == "no_timing" else ic
            nav, info = run_backtest(panel, ap, dates, cfg, ic_arg,
                                     pit_members=pit_map if is_a0 else None)
            nav = nav[nav.index >= ws]  # 只计窗口段
            cm = calc_metrics(nav)
            anns.append(float(cm["年化_float"]))
            rets.append(nav.pct_change().dropna())
            tos.append(info["total_commission"] / cfg.commission / years)
        j = pd.concat(rets, axis=1).dropna()
        ens = (1 + j.mean(axis=1)).cumprod()
        ecm = calc_metrics(ens)
        results[name] = {
            "path_mean": float(np.mean(anns)), "ens_ann": float(ecm["年化_float"]),
            "sharpe": float(ecm["夏普_float"]), "dd": float(ecm["回撤_float"]),
            "turnover": float(np.mean(tos)),
        }
    return results


def main():
    import argparse
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--window", default="both", choices=["main", "oos", "both"])
    ap_.add_argument("--paths", type=int, default=10, help="路径数(默认10, 冒烟测试可减)")
    ap_.add_argument("--csv-out", default="logs/attribution_v2_prewarm.csv",
                     help="结果CSV落盘路径(T2产物)")
    ap_.add_argument("--no-prewarm", action="store_true",
                     help="T2 A/B对照: 面板从窗口起点加载(旧暖机语义), "
                          "隔离预加载净效应")
    ap_.add_argument("--a0-legacy", action="store_true",
                     help="追加旧独立A0臂作对比(T3对齐自检用)")
    args = ap_.parse_args()

    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    pit = load_pit_memberships()
    entry_map = load_entry_map()
    csv_rows = []

    for wname in (["main", "oos"] if args.window == "both" else [args.window]):
        START, END = WINDOWS[wname]
        print(f"\n{'='*76}\n窗口 {wname} ({START} ~ {END})\n{'='*76}", flush=True)
        # 预加载: 面板起点前推400日历日(≈270交易日>min_bars=250),
        # 指标从窗口首日即有值, 首日即可调仓; 严禁前视——只允许用
        # 窗口起点之前的历史算指标, 引擎语义不变(2026-09-08 T2)
        LOAD_START = (START if args.no_prewarm else
                      (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d"))
        prices, amounts = {}, {}
        for code in codes:
            try:
                d = load_daily(code, LOAD_START, END)
                if d.empty:
                    continue
                d["date"] = pd.to_datetime(d["date"])
                d = d.set_index("date").sort_index()
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
        ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
        ic.index = pd.to_datetime(ic.index)
        # 日历从预加载起点开始(不早于2014-06-01也行, 但必须覆盖LOAD_START,
        # 否则面板∩日历会把OOS预加载段(2013-11起)全删→min_bars退回2015-06,
        # 2026-09-08 v3首轮实锤: OOS A2 -8.8%→-17.6%的根因)
        sh = load_daily("SH000001", LOAD_START, END)
        cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
        # 面板索引∩交易日历: 剔除假期行(603012等新浪源假期行会进面板索引,
        # 造成全市场NaN日→稀释年化~0.3pp且污染停牌检测; 2026-09-08 T4发现)
        panel = panel[panel.index.isin(pd.to_datetime(cal))]
        ap = ap[ap.index.isin(pd.to_datetime(cal))]
        base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
        idx = {d: i for i, d in enumerate(cal)}

        def path(off):
            shifted = [cal[idx.get(d, 0) + off] for d in base
                       if idx.get(d, 0) + off < len(cal)]
            return [d for d in shifted if START <= d <= END]

        rebal_paths = [path(off) for off in range(args.paths)]
        years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25  # 窗口年数(非面板跨度)
        use_pit = pit if wname == "main" else None

        # 旧独立A0(--a0-legacy): 只跑窗口段, 无暖机语义故切片
        a0_legacy_ecm = None
        if args.a0_legacy:
            panel_win = panel[panel.index >= pd.Timestamp(START)]
            a0_anns, a0_turns, a0_ecm = run_a0(panel_win, cal, rebal_paths,
                                               use_pit, entry_map)
            a0_legacy_ecm = a0_ecm
            a0_mean = float(np.mean(a0_anns)) if a0_anns else float("nan")
            a0_turn = float(np.mean(a0_turns)) if a0_turns else 0.0
            print(f"A0_legacy独立实现: 路径均值{a0_mean*100:+.2f}% "
                  f"摊平{a0_ecm['年化_float']*100:+.2f}% 夏普{a0_ecm['夏普_float']:.2f} "
                  f"回撤{a0_ecm['回撤_float']*100:.2f}% 换手{a0_turn/years*100:.0f}%/年",
                  flush=True)
            csv_rows.append({"window": wname, "arm": "A0_legacy独立实现",
                             "path_mean": a0_mean,
                             "ens_ann": float(a0_ecm["年化_float"]),
                             "sharpe": float(a0_ecm["夏普_float"]),
                             "dd": float(a0_ecm["回撤_float"]),
                             "turnover": a0_turn / years})

        # A0(T3统一) + A1/A2/A3 引擎; A0 池=PIT成员(每调仓日预计算)
        all_dates = sorted({d for dates in rebal_paths for d in dates})
        pit_map = {d: sorted(members_on(d, use_pit, entry_map) or [])
                   for d in all_dates}
        res = run_engine_arms(panel, ap, ic, cal, rebal_paths, years, START,
                              pit_map=pit_map)
        for name, r in res.items():
            print(f"{name:<20}: 路径均值{r['path_mean']*100:+.2f}% "
                  f"摊平{r['ens_ann']*100:+.2f}% 夏普{r['sharpe']:.2f} "
                  f"回撤{r['dd']*100:.2f}% 换手{r['turnover']*100:.0f}%/年",
                  flush=True)
            csv_rows.append({"window": wname, "arm": name,
                             **{k: (round(v, 6) if isinstance(v, float) else v)
                                for k, v in r.items()}})

        # 分解
        a0u = res["A0_800等权buyhold"]
        a1, a2, a3 = (res["A1_pool30买持"], res["A2_pool30+五档"],
                      res["A3_完整栈"])
        decomp = {"池贡献": a1["ens_ann"] - a0u["ens_ann"],
                  "择时贡献": a2["ens_ann"] - a1["ens_ann"],
                  "组件贡献": a3["ens_ann"] - a2["ens_ann"]}
        print(f"\n分解(摊平年化, A0=T3同引擎退化臂): "
              f"池贡献={decomp['池贡献']:+.2%} | "
              f"择时贡献={decomp['择时贡献']:+.2%} | "
              f"组件贡献={decomp['组件贡献']:+.2%}", flush=True)
        csv_rows.append({"window": wname, "arm": "分解",
                         **{k: round(v, 6) for k, v in decomp.items()}})

        # §6.8 一致性检查(A2=B最小栈 +4.33%, A3=A +4.43%): 常量基于旧
        # 暖机语义, 仅在 --no-prewarm 对照下适用; 预加载下 2019 年入池,
        # 偏离属预期(2026-09-08 T2)
        if wname == "main":
            d2, d3 = (a2["ens_ann"] - 0.0433), (a3["ens_ann"] - 0.0443)
            verdict = "OK" if abs(d2) <= 0.005 and abs(d3) <= 0.005 \
                else "(|Δ|>0.5% 需查口径)"
            if args.no_prewarm:
                print(f"§6.8 复现检查(对照语义): A2偏离{d2:+.2%}, "
                      f"A3偏离{d3:+.2%} {verdict}", flush=True)
            else:
                print(f"§6.8 复现检查(预加载下预期偏离): A2 {d2:+.2%}, "
                      f"A3 {d3:+.2%}", flush=True)

    if csv_rows:
        pd.DataFrame(csv_rows).to_csv(args.csv_out, index=False,
                                      encoding="utf-8-sig")
        print(f"\n结果已落盘: {args.csv_out}", flush=True)


if __name__ == "__main__":
    main()
