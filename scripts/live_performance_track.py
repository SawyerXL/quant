"""实盘/仿真 NAV 与 40/60 回测的同窗跟踪（只读，日更链外独立跑）。

为什么需要：项目一直在引用 11.97% 的长期回测年化，但仿真从 2026-07-20
才起步；必须回答“同一段窗口里，实盘相对回测跟得怎么样”，而不是拿
3 个月绝对收益去对 7 年年化。口径：
  - live: logs/qmt_nav_history.parquet（QMT 快照 mark-to-market，官方仿真 NAV）
  - backtest: logs/combo_nav_20w_two_group.csv（40/60 live{2,7} 两组回测）
  - CB 指数: data_store/meta/cb_index.parquet（价格口径，仅参考）
对齐到两者共同交易日，各自在共同起点归一为 1.0；输出累计、回撤、
年化（样本 <20 日不算）、跟踪差、跟踪误差、月度差、暴露/持仓数。
快照陈旧时标 stale，不把陈旧值当今日结果。

用法: python scripts/live_performance_track.py
产出: logs/live_performance_track.json / .md
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "logs" / "qmt_nav_history.parquet"
BT = ROOT / "logs" / "combo_nav_20w_two_group.csv"
CB_INDEX = ROOT / "data_store" / "meta" / "cb_index.parquet"
DAILY_STUDY = ROOT / "logs" / "market_daily_study.csv"
OUT_JSON = ROOT / "logs" / "live_performance_track.json"
OUT_MD = ROOT / "logs" / "live_performance_track.md"
NOTIONAL = 1_600_000.0


def load_live(path: Path = LIVE) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").set_index("date")


def load_backtest(path: Path = BT) -> pd.Series:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    col = "nav" if "nav" in df.columns else df.columns[0]
    return pd.to_numeric(df[col], errors="coerce").dropna().sort_index()


def normalize(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").dropna().astype(float)
    if s.empty or s.iloc[0] == 0:
        return s
    return s / s.iloc[0]


def max_drawdown(s: pd.Series) -> float:
    s = normalize(s)
    if s.empty:
        return float("nan")
    return float((s / s.cummax() - 1).min())


def metrics(s: pd.Series) -> dict:
    s = normalize(s)
    n = len(s)
    if n < 2:
        return {"n": n}
    ret = s.pct_change().dropna()
    cum = float(s.iloc[-1] - 1)
    ann = float((1 + cum) ** (252 / max(n - 1, 1)) - 1) if n > 20 else None
    vol = float(ret.std() * np.sqrt(252)) if len(ret) > 1 else None
    sharpe = (
        float(ret.mean() / ret.std() * np.sqrt(252))
        if len(ret) > 1 and ret.std() > 0
        else None
    )
    return {
        "n": n,
        "cum_pct": round(cum * 100, 3),
        "ann_pct": round(ann * 100, 3) if ann is not None else None,
        "vol_pct": round(vol * 100, 3) if vol is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "max_dd_pct": round(max_drawdown(s) * 100, 3),
        "annualized_meaningful": bool(n > 20),
    }


def tracking(live: pd.Series, bt: pd.Series) -> dict:
    j = pd.concat(
        [pd.to_numeric(live, errors="coerce").rename("live"),
         pd.to_numeric(bt, errors="coerce").rename("bt")],
        axis=1, join="inner",
    ).dropna()
    if len(j) < 2:
        return {"n": len(j), "error": "共同交易日不足"}
    j = j / j.iloc[0]
    lr = j["live"].pct_change().dropna()
    br = j["bt"].pct_change().dropna()
    diff = (lr - br).dropna()
    cum_live = float(j["live"].iloc[-1] - 1)
    cum_bt = float(j["bt"].iloc[-1] - 1)
    monthly = j.resample("ME").last().pct_change().dropna()
    mdiff = (monthly["live"] - monthly["bt"]).dropna()
    beta = alpha_ann = corr = None
    if len(diff) > 2 and float(br.var()) > 0:
        cov = np.cov(lr, br, ddof=1)
        beta = float(cov[0, 1] / cov[1, 1])
        alpha_ann = float((lr.mean() - beta * br.mean()) * 252)
        corr = float(lr.corr(br))
    return {
        "n": len(j),
        "start": str(j.index[0].date()),
        "end": str(j.index[-1].date()),
        "cum_live_pct": round(cum_live * 100, 3),
        "cum_bt_pct": round(cum_bt * 100, 3),
        "excess_pp": round((cum_live - cum_bt) * 100, 3),
        "beta": round(beta, 3) if beta is not None else None,
        "alpha_ann_pct": round(alpha_ann * 100, 3) if alpha_ann is not None else None,
        "corr": round(corr, 3) if corr is not None else None,
        "te_ann_pct": round(float(diff.std() * np.sqrt(252) * 100), 3)
        if len(diff) > 1 else None,
        "tracking_diff_ann_pct": round(float(diff.mean() * 252 * 100), 3)
        if len(diff) > 1 else None,
        "monthly_diff_pp": {
            str(k.date()): round(float(v) * 100, 3) for k, v in mdiff.items()
        },
        "live": metrics(j["live"]),
        "bt": metrics(j["bt"]),
    }


def cb_vs_index(live: pd.DataFrame) -> dict:
    if "nav_cb" not in live.columns or not CB_INDEX.exists():
        return {"error": "缺 nav_cb 或 CB 指数"}
    idx = pd.read_parquet(CB_INDEX)
    idx["date"] = pd.to_datetime(idx["date"])
    s_idx = pd.to_numeric(idx.set_index("date")["close"], errors="coerce").dropna()
    j = pd.concat([live["nav_cb"].rename("cb"), s_idx.rename("idx")],
                  axis=1, join="inner").dropna()
    if len(j) < 2:
        return {"n": len(j), "error": "共同交易日不足"}
    j = j / j.iloc[0]
    cum_cb = float(j["cb"].iloc[-1] - 1)
    cum_idx = float(j["idx"].iloc[-1] - 1)
    lr = j["cb"].pct_change().dropna()
    ir = j["idx"].pct_change().dropna()
    beta = alpha_ann = corr = None
    if len(lr) > 2 and float(ir.var()) > 0:
        cov = np.cov(lr, ir, ddof=1)
        beta = float(cov[0, 1] / cov[1, 1])
        alpha_ann = float((lr.mean() - beta * ir.mean()) * 252)
        corr = float(lr.corr(ir))
    return {
        "n": len(j),
        "start": str(j.index[0].date()),
        "end": str(j.index[-1].date()),
        "cb_cum_pct": round(cum_cb * 100, 3),
        "index_cum_pct": round(cum_idx * 100, 3),
        "relative_pp": round((cum_cb - cum_idx) * 100, 3),
        "beta": round(beta, 3) if beta is not None else None,
        "alpha_ann_pct": round(alpha_ann * 100, 3) if alpha_ann is not None else None,
        "corr": round(corr, 3) if corr is not None else None,
        "benchmark_basis": "price_close; 全市场 CB 指数，非策略同宇宙",
        "caveats": "价格口径/无分红；指数只到 10-23；单路径",
    }


def exposure_segments(live: pd.DataFrame) -> dict:
    """按股票腿是否持仓拆分，归因 nav_a/nav_cb/nav_combined 的日收益。

    只看段内发生的日收益（pct_change 后按 mask 取），不是两端的净值差；
    这样不会把段外的涨跌算进来。"""
    out = {}
    for label, mask in (
        ("stock_active", live["n_pos_a"] > 1),
        ("cash_only", live["n_pos_a"] <= 1),
    ):
        x = live.loc[mask]
        if len(x) < 2:
            continue
        r = x[["nav_a", "nav_cb", "nav_combined"]].pct_change().dropna()
        if r.empty:
            continue
        out[label] = {
            "n_days": int(len(x)),
            "n_ret_days": int(len(r)),
            "start": str(x.index[0].date()),
            "end": str(x.index[-1].date()),
            "nav_a_pct": round(float((1 + r["nav_a"]).prod() - 1) * 100, 3),
            "nav_cb_pct": round(float((1 + r["nav_cb"]).prod() - 1) * 100, 3),
            "nav_combined_pct": round(
                float((1 + r["nav_combined"]).prod() - 1) * 100, 3
            ),
        }
    return out


def expected_last_bar() -> str | None:
    if not DAILY_STUDY.exists():
        return None
    try:
        d = pd.read_csv(DAILY_STUDY, usecols=["date"])
        return str(pd.to_datetime(d["date"]).max().date())
    except Exception:
        return None


def build(live_path: Path = LIVE, bt_path: Path = BT) -> dict:
    live = load_live(live_path)
    bt = load_backtest(bt_path)
    as_of = str(live.index[-1].date())
    exp = expected_last_bar()
    stale = bool(exp and as_of < exp)
    start = "2026-07-20"
    live_w = live.loc[start:]
    bt_w = bt.loc[start:]
    tr = tracking(live_w["nav_combined"], bt_w)
    # 回测结束后的 live 尾巴，单独报，不混入跟踪差
    tail = {}
    if len(bt_w):
        bt_end = bt_w.index[-1]
        after = live_w.loc[live_w.index > bt_end, "nav_combined"]
        if len(after) >= 2:
            tail = {
                "start": str(after.index[0].date()),
                "end": str(after.index[-1].date()),
                "cum_pct": round(float(after.iloc[-1] / after.iloc[0] - 1) * 100, 3),
            }
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": as_of,
        "expected_last_bar": exp,
        "stale": stale,
        "window": {"start": start, "live_end": as_of, "bt_end": str(bt.index[-1].date())},
        "live_source": str(LIVE.relative_to(ROOT)),
        "backtest_source": str(BT.relative_to(ROOT)),
        "tracking": tr,
        "live_after_bt_end": tail,
        "exposure_segments": exposure_segments(live_w),
        "cb_vs_index": cb_vs_index(live_w),
        "exposure": {
            "last_invested_combined": float(live_w["invested_combined"].iloc[-1]),
            "last_invested_pct": round(
                float(live_w["invested_combined"].iloc[-1]) / NOTIONAL * 100, 2
            ),
            "avg_invested_pct": round(
                float((live_w["invested_combined"] / NOTIONAL).mean() * 100), 2
            ),
            "min_n_pos_combined": int(live_w["n_pos_combined"].min()),
            "max_n_pos_combined": int(live_w["n_pos_combined"].max()),
            "last_n_pos_combined": int(live_w["n_pos_combined"].iloc[-1]),
        },
        "caveats": [
            "live 为 QMT 仿真快照 mark-to-market，非真实成交；",
            "backtest 为 40/60 live{2,7} 两组、2019-2026 回测，窗口只切 2026-07-20 起；",
            "共同交易日到回测末端 10-19；10-20 之后 live 尾巴单列；",
            "年化在 <20 日不报；样本短，仅跟踪用，不是业绩结论；",
            "live 股票腿暴露在窗口内变化（8 月 25~32 只 → 10 月 1 只 511360），"
            "风险/收益对比未按暴露归因；回测是固定 40/60 路径；",
            "CB 指数为价格口径/全市场宇宙，不能替代策略同宇宙总收益基准。",
        ],
    }
    return out


def render_md(d: dict) -> str:
    tr = d.get("tracking", {})
    cb = d.get("cb_vs_index", {})
    lines = [
        "# 实盘/仿真 vs 40/60 回测跟踪",
        "",
        f"- 生成：{d['generated_at']}；as_of：**{d['as_of']}**；"
        f"回测末端：{d['window']['bt_end']}；stale：**{d['stale']}**",
        f"- 窗口：{tr.get('start')} → {tr.get('end')}，共同交易日 **{tr.get('n')}**",
        "",
        "## 同窗累计",
        "| 口径 | 累计 | 年化 | 最大回撤 |",
        "|---|---:|---:|---:|",
    ]
    for name, key in (("仿真 live", "live"), ("40/60 回测", "bt")):
        m = tr.get(key, {})
        lines.append(
            f"| {name} | {m.get('cum_pct')}% | {m.get('ann_pct')}% | {m.get('max_dd_pct')}% |"
        )
    lines += [
        "",
        f"- **跟踪差（累计）**：{tr.get('excess_pp')}pp；"
        f"跟踪误差（年化）：{tr.get('te_ann_pct')}%；"
        f"年化跟踪差：{tr.get('tracking_diff_ann_pct')}%",
        f"- 回归：beta={tr.get('beta')}，alpha(年化)={tr.get('alpha_ann_pct')}%，"
        f"corr={tr.get('corr')}",
        f"- 回测结束后 live 尾巴：{d.get('live_after_bt_end', {})}",
        f"- 暴露：平均投入 {d['exposure']['avg_invested_pct']}%，"
        f"最新 {d['exposure']['last_invested_pct']}%；持仓 "
        f"{d['exposure']['min_n_pos_combined']}~"
        f"{d['exposure']['max_n_pos_combined']} 只（最新 "
        f"{d['exposure']['last_n_pos_combined']}）",
        *[
            f"- {label}: {e['start']}→{e['end']} n={e['n_days']}；"
            f"nav_a {e['nav_a_pct']}% / nav_cb {e['nav_cb_pct']}% / "
            f"combined {e['nav_combined_pct']}%"
            for label, e in d.get("exposure_segments", {}).items()
        ],
        "",
        "## CB 腿 vs CB 指数",
        f"- 同窗 {cb.get('start')} → {cb.get('end')}（n={cb.get('n')}）："
        f"CB 腿 {cb.get('cb_cum_pct')}% vs 指数 {cb.get('index_cum_pct')}%，"
        f"相对 {cb.get('relative_pp')}pp",
        f"- CB 回归：beta={cb.get('beta')}，"
        f"alpha(年化)={cb.get('alpha_ann_pct')}%，corr={cb.get('corr')}",
        f"- 口径：{cb.get('benchmark_basis')}；{cb.get('caveats')}",
        "",
        "## 局限",
    ]
    lines += [f"- {x}" for x in d.get("caveats", [])]
    return "\n".join(lines) + "\n"


def main() -> int:
    d = build()
    OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_MD.write_text(render_md(d), encoding="utf-8")
    logger.info(f"live tracking: as_of={d['as_of']} stale={d['stale']} "
                f"tracking={d['tracking']}")
    print(render_md(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
