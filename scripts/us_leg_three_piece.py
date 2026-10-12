"""美股腿三件套预览版 (2026-10-12 晚, 原排期 10-13 晚提前执行, 用户裁定)。

⚠ 全数挂"待时点化复核"标签: 预览继承静态评级偏差(评级字段 PIT 问题),
终版在时点化后网格重跑。

口径声明:
  - 腿收益: logs/stock_cb_joint_returns.csv (s=股票腿 cb=CB腿, 定稿同源,
    as_of 2026-09-18, 2019-01 起) + 纳指 .IXIC 新浪源(10-12 拉取缓存
    data_store/meta/us_ixic_sina.parquet, 美东 T-1 收盘收益对齐中国 T 日,
    时区因果口径与 us_corr_probe 一致)。
  - 组合: 基线=40/60(定稿配比); 美股腿 u∈{10%,12.5%,15%}×资金口径{内切:
    总 160 万不变, 股:CB 按 40:60 分摊剩余 / 新增: 160 万两腿不变+US 追加
    u×160 万}。⚠ 部署口径(100万股票:60万CB=62.5:37.5)与定稿 40/60 的差异
    不在本预览处理, 终版裁定。
  - Part A(画像+熔断网格): 日收益算术, 无 lot/无交易成本模型(两腿收益已
    含各自成本口径)。OOS 2015-2018 无组合级数据(joint CSV 2019 起), 标
    终版补。
  - Part B(跳票率/覆盖率): 引擎复跑 backtest_engine.run_backtest,
    DEFAULT_CONFIG lot_size=100 × 内切后股票腿每组分资金 {27.2/28.8/32/50}万,
    主窗 2019~2026-10-09; OOS 薄数据终版补。
  - 熔断近似(终版用引擎): 触发=组合回撤下穿 −τ; 穿透深度=max(超过阈值
    部分); B方案近似=触发后冻结当时权重至回撤回到 −τ+2pp(不重平衡),
    伤害=冻结臂 vs 无熔断臂之差。
用法: python scripts/us_leg_three_piece.py --part A   (轻, 立即出)
      python scripts/us_leg_three_piece.py --part B   (重, 后台跑)
      python scripts/us_leg_three_piece.py --part all
"""
import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

END = "2026-10-09"
START = "2019-01-01"
US_FRACTIONS = [0.10, 0.125, 0.15]
HALT_THRESH = [-0.10, -0.15, -0.20, -0.25]
OUT = Path("logs/us_leg_three_piece_result.json")


def metrics_of(ret: pd.Series) -> dict:
    """年化/夏普/回撤 (日收益口径, 252 交易日)。"""
    nav = (1 + ret).cumprod()
    ar = (nav.iloc[-1] ** (252 / len(nav)) - 1)
    sr = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0.0
    dd = (nav / nav.cummax() - 1).min()
    return {"年化": round(float(ar) * 100, 2), "夏普": round(float(sr), 3),
            "回撤": round(float(dd) * 100, 2)}


def load_legs():
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
    joint.columns = ["s", "cb"]
    us = pd.read_parquet("data_store/meta/us_ixic_sina.parquet")
    us["date"] = pd.to_datetime(us["date"])
    us = us.set_index("date")["close"].astype(float).sort_index()
    us_ret = us.pct_change().dropna()
    # 时区因果对齐: 中国 T 日取美东 ≤T-1 的最新收盘收益
    aligned = {}
    us_dates = us_ret.index
    for t in joint.index:
        cand = us_dates[us_dates < t]
        if len(cand):
            aligned[t] = us_ret.loc[cand[-1]]
    us_al = pd.Series(aligned).reindex(joint.index)
    legs = pd.DataFrame({"s": joint["s"], "cb": joint["cb"], "us": us_al}).dropna()
    return legs


def combo_ret(legs, w_s, w_cb, w_us):
    return w_s * legs["s"] + w_cb * legs["cb"] + w_us * legs["us"]


def part_a():
    legs = load_legs()
    print(f"窗口 {legs.index[0].date()}~{legs.index[-1].date()} ({len(legs)} 日) | "
          f"腿均值: 股票{legs['s'].mean()*100:.3f}% CB{legs['cb'].mean()*100:.3f}% "
          f"纳指{legs['us'].mean()*100:.3f}%")
    arms = {}
    arms["基线 40/60"] = combo_ret(legs, 0.4, 0.6, 0)
    for u in US_FRACTIONS:
        arms[f"内切 US{u*100:.1f}%"] = combo_ret(legs, 0.4 * (1 - u), 0.6 * (1 - u), u)
        arms[f"新增 US{u*100:.1f}%"] = (0.4 * legs["s"] + 0.6 * legs["cb"] + u * legs["us"]) / (1 + u)
    print(f"\n{'臂':<18}{'年化':>8}{'夏普':>7}{'回撤':>9}")
    rows = {}
    for name, ret in arms.items():
        m = metrics_of(ret)
        rows[name] = m
        print(f"{name:<18}{m['年化']:>+7.2f}%{m['夏普']:>7.3f}{m['回撤']:>+8.2f}%")
    # 熔断网格(近似, 见 docstring): 触发计数+穿透深度; B方案伤害测量
    # 需引擎级重平衡语义, 组合层恒定权重下冻结=无操作, 留终版。
    print(f"\n{'臂×阈值':<24}{'触发次数':>8}{'穿透深度max':>12}")
    halt_rows = {}
    for name, ret in arms.items():
        nav = (1 + ret).cumprod()
        for tau in HALT_THRESH:
            dd = nav / nav.cummax() - 1
            triggered = dd <= tau
            n = int((triggered & ~triggered.shift(1, fill_value=False)).sum())
            pen = float((dd[triggered] - tau).min()) * 100 if n else 0.0
            halt_rows[f"{name} τ{tau*100:.0f}%"] = {"触发": n, "穿透pp": round(pen, 2)}
            print(f"{name} τ{tau*100:>5.0f}%{n:>9}{pen:>+11.2f}pp")
    return {"arms": rows, "halt_grid": halt_rows,
            "window": [str(legs.index[0].date()), str(legs.index[-1].date())],
            "label": "待时点化复核(预览版)",
            "note": "B方案伤害测量留终版引擎(组合层恒定权重, 冻结无对应操作)"}


def part_b():
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只, 调仓日 {len(rebal)}个")
    # 内切后股票腿每组分资金: u=10%→28.8万, u=12.5%→28万, u=15%→27.2万; 现状50万
    caps = [272_000, 280_000, 288_000, 500_000]
    print(f"\n{'资金/组':<16}{'年化':>9}{'夏普':>7}{'回撤':>9}{'跳票':>7}")
    rows = {}
    base = replace(DEFAULT_CONFIG, lot_size=0, initial_capital=caps[0])
    nav0, m0 = run_backtest(panel, ap, rebal, base, idx_c)
    cm0 = calc_metrics(nav0)
    rows["无约束"] = {"年化": cm0["年化收益率"], "夏普": cm0["夏普比率"], "回撤": cm0["最大回撤"], "跳票": m0.get("lot_skips", 0)}
    print(f"{'无约束(无限可分)':<16}{cm0['年化收益率']:>9}{cm0['夏普比率']:>7}{cm0['最大回撤']:>9}{0:>7}")
    for cap in caps:
        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        cm = calc_metrics(nav)
        skip = m.get("lot_skips", m.get("skips", 0))
        rows[f"{cap/10000:.1f}万/组"] = {"年化": cm["年化收益率"], "夏普": cm["夏普比率"],
                                          "回撤": cm["最大回撤"], "跳票": skip}
        print(f"{'lot '+f'{cap/10000:.1f}万/组':<16}{cm['年化收益率']:>9}{cm['夏普比率']:>7}{cm['最大回撤']:>9}{skip:>7}")
    return {"part_b": rows, "label": "待时点化复核(预览版, 主窗; OOS 终版补)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="A", choices=["A", "B", "all"])
    args = ap.parse_args()
    result = {}
    if args.part in ("A", "all"):
        result.update(part_a())
    if args.part in ("B", "all"):
        result.update(part_b())
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    print(f"\n已保存 {OUT}")


if __name__ == "__main__":
    main()
