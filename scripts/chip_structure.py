"""筹码结构 / "洗盘"形态 — 个股研究模块（2026-10-25）

为什么：用户提出的"庄家拉升前洗盘"叙事里，只有三条能直接观测的约束——
累积换手、区间/时长、大盘环境；"庄家持仓 30-50%" 在现代 A 股不可合法观测
（5% 权益披露、30% 要约线），所以只能用**筹码分布代理 + 换手 + 形态**去逼近。
本模块给个人个股研究提供"洗盘候选 / 出货嫌疑 / 不适用"的**观察项**，
不是买卖信号，不进 QMT 信号链（架构原则：个人研究 ≠ 执行链）。

数据与代理（全部可复算）：
- 日线：本地 `data.store.load_daily`（volume=万股，amount=万元，比值=元/股）
- 流通股本：akshare `stock_value_em`（东财，2018-01 起），按票缓存到
  `data_store/meta/float_shares/<code>.parquet`
- 筹码分布：日线近似——把每天成交量按 [low, high] 均匀铺到价格分箱，
  按 60 日半衰期衰减（越近的成交越代表当前筹码）。不是交易所/东财真值，
  盲区：日内路径、大宗/协议转让、融资盘成本、限售解禁都不在日线里。
- 官方筹码（MCP `cyq-perf`）只有最近 ≤20 个交易日（FREE 档），
  只能用于前向核对，不能回测；`holder-number` 需 PRO（403）。见
  `docs/washout_study_plan.md` 数据可行性一节。

状态机（参数全部集中在本文件顶部，历史研究用同一套）：
  无前期拉升 -> JUST_HIGH -> BROKEN -> OVER_3M -> RANGE_TOO_WIDE
  -> FLOAT_UNAVAILABLE -> TURNOVER_LOW -> WASHOUT_WATCH
WASHOUT_WATCH 只是"形态齐全"，是否可看还要 regime_ok（基准在 MA200 上）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CACHE_DIR = ROOT / "data_store" / "meta" / "float_shares"
LOG_DIR = ROOT / "logs"

# ── 形态参数（改这里 = 改判据；历史研究必须引用同一组）────────────────
P = {
    "lookback_days": 520,       # 载入日历天
    "high_window": 250,         # 找前期高点/涨幅的交易日窗口
    "prior_leg_min": 0.30,      # 前期拉升最小涨幅（低点->高点）
    "days_min": 10,             # 距高点最少交易日（太近=还在拉升）
    "days_max": 60,             # 3 个月≈60 交易日（超过=出货/蓄势不可分辨）
    "dd_min": -0.30,            # 回调下限（再深=破位不是横盘）
    "dd_max": -0.08,            # 回调上限（太浅=没洗）
    "range_width_max": 0.25,    # 横盘区间宽度上限
    "turnover_min": 0.30,       # 洗盘期累积换手（流通盘）下限
    "chip_window": 120,         # 筹码分布回看窗口
    "chip_half_life": 60,       # 筹码衰减半衰期（交易日）
    "chip_bins": 60,
    "vol_shrink_window": 10,    # 缩量：近 10 日均量 / 前 40 日均量
    "vol_shrink_ref": 40,
    "vwap_tol": 0.02,           # amount/volume 落在 [low,high] 的容差
}


def _to_dt(s):
    return pd.to_datetime(s)


def _is_fund(code: str) -> bool:
    """ETF/LOF：5xx(沪)、15/16/159(深)。11xxxx 是可转债，不走本框架。"""
    return code.startswith("5") or code.startswith("15") or code.startswith("16")


def load_bars(code: str, asof: str, lookback_days: int | None = None) -> pd.DataFrame | None:
    """本地日线；最后一行必须 <= asof（不静默用更晚的 bar）。"""
    from data.storage import load_daily
    lb = lookback_days or P["lookback_days"]
    start = (pd.Timestamp(asof) - pd.Timedelta(days=lb)).strftime("%Y-%m-%d")
    d = load_daily(code, start, asof)
    if d is None or len(d) == 0:
        return None
    d = d.copy()
    d["date"] = _to_dt(d["date"])
    d = d[d["date"] <= pd.Timestamp(asof)].sort_values("date").reset_index(drop=True)
    return d if len(d) else None


def load_float_shares(code: str, asof: str | None = None) -> tuple[pd.DataFrame | None, str]:
    """流通股本历史（股）。返回 (df, status)；status ∈ {ok, cache_ok, fetched, missing, error}。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{code}.parquet"
    d = None
    if p.exists():
        try:
            d = pd.read_parquet(p)
            d["date"] = _to_dt(d["date"])
        except Exception as e:  # 缓存损坏 → 重拉
            logger.warning(f"float cache read fail {code}: {e}")
            d = None
    need = d is None or len(d) == 0
    if not need and asof is not None:
        need = d["date"].max() < pd.Timestamp(asof)
    if not need and asof is None:
        need = (date.today() - d["date"].max().date()).days > 10
    if not need:
        return d, "cache_ok"
    try:
        import akshare as ak
        raw = ak.stock_value_em(symbol=code)
        if raw is None or raw.empty:
            return d, "missing"
        x = raw[["数据日期", "流通股本"]].rename(columns={"数据日期": "date", "流通股本": "float_shares"})
        x["date"] = _to_dt(x["date"])
        x["float_shares"] = pd.to_numeric(x["float_shares"], errors="coerce")
        x = x.dropna().drop_duplicates("date").sort_values("date").reset_index(drop=True)
        if len(x) == 0:
            return d, "missing"
        x.to_parquet(p, index=False)
        return x, "fetched"
    except Exception as e:
        logger.warning(f"float fetch fail {code}: {type(e).__name__} {str(e)[:100]}")
        return d, ("cache_ok" if d is not None and len(d) else "error")


def attach_float(bars: pd.DataFrame, fs: pd.DataFrame | None) -> pd.DataFrame:
    d = bars.copy()
    if fs is None or len(fs) == 0:
        d["float_shares"] = np.nan
    else:
        f = fs.drop_duplicates("date").sort_values("date")
        d = pd.merge_asof(d.sort_values("date"), f, on="date", direction="backward")
    return d


def cum_turnover(d: pd.DataFrame, i0: int, i1: int) -> tuple[float | None, int]:
    """[i0,i1] 区间累积换手（流通盘）；返回 (turnover, 有效天数)。缺流通股本的日子跳过并计数。"""
    sub = d.iloc[i0:i1 + 1]
    v = pd.to_numeric(sub["volume"], errors="coerce")
    fs = pd.to_numeric(sub["float_shares"], errors="coerce")
    ok = (v > 0) & (fs > 0)
    if ok.sum() == 0:
        return None, 0
    # volume 万股 -> 股
    return float(((v[ok] * 1e4) / fs[ok]).sum()), int(ok.sum())


def chip_distribution(d: pd.DataFrame, window: int | None = None,
                      half_life: int | None = None, bins: int | None = None) -> dict | None:
    """日线近似筹码分布。返回 cost_5/15/50/85/95、winner_rate、width90、vwap_ok_ratio。"""
    window = window or P["chip_window"]
    half_life = half_life or P["chip_half_life"]
    bins = bins or P["chip_bins"]
    x = d.tail(window).reset_index(drop=True)
    if len(x) < 20:
        return None
    v = pd.to_numeric(x["volume"], errors="coerce").fillna(0).to_numpy(float)
    a = pd.to_numeric(x["amount"], errors="coerce").fillna(0).to_numpy(float)
    lo = pd.to_numeric(x["low"], errors="coerce").to_numpy(float)
    hi = pd.to_numeric(x["high"], errors="coerce").to_numpy(float)
    cl = pd.to_numeric(x["close"], errors="coerce").to_numpy(float)
    if not np.isfinite(cl[-1]) or not np.isfinite(lo).any() or not np.isfinite(hi).any():
        return None
    vwap = np.where(v > 0, a / np.where(v == 0, 1, v), (lo + hi + cl) / 3)
    ok = (vwap >= lo * (1 - P["vwap_tol"])) & (vwap <= hi * (1 + P["vwap_tol"]))
    vwap = np.where(ok, vwap, (lo + hi + cl) / 3)
    age = np.arange(len(x))[::-1]
    w = v * (0.5 ** (age / float(half_life)))
    pmin, pmax = np.nanmin(lo), np.nanmax(hi)
    if not np.isfinite(pmin) or not np.isfinite(pmax) or pmax <= pmin:
        return None
    edges = np.linspace(pmin, pmax, bins + 1)
    hist = np.zeros(bins)
    for i in range(len(x)):
        if not np.isfinite(w[i]) or w[i] <= 0 or not np.isfinite(lo[i]) or not np.isfinite(hi[i]):
            continue
        l, h = max(lo[i], pmin), min(hi[i], pmax)
        if h <= l:
            idx = min(max(int(np.searchsorted(edges, vwap[i]) - 1), 0), bins - 1)
            hist[idx] += w[i]
            continue
        i0 = min(max(int(np.searchsorted(edges, l) - 1), 0), bins - 1)
        i1 = min(max(int(np.searchsorted(edges, h) - 1), 0), bins - 1)
        if i1 < i0:
            i1 = i0
        hist[i0:i1 + 1] += w[i] / (i1 - i0 + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    cw = np.cumsum(hist)
    tot = cw[-1]
    if not np.isfinite(tot) or tot <= 0:
        return None

    def q(p):
        return float(np.interp(p * tot, cw, centers))

    c5, c15, c50, c85, c95 = q(.05), q(.15), q(.50), q(.85), q(.95)
    return {
        "cost_5": c5, "cost_15": c15, "cost_50": c50, "cost_85": c85, "cost_95": c95,
        "width90": (c95 - c5) / c50 if c50 > 0 else None,
        "width70": (c85 - c15) / c50 if c50 > 0 else None,
        "winner_rate": float(hist[centers <= cl[-1]].sum() / tot),
        "vwap_ok_ratio": float(ok.mean()),
        "chip_window": len(x),
    }


def bench_regime(bench: str, asof: str) -> dict:
    b = load_bars(bench, asof, 400)
    if b is None or len(b) < 200:
        return {"ok": False, "reason": f"bench {bench} bars<200"}
    ma200 = float(pd.to_numeric(b["close"], errors="coerce").tail(200).mean())
    close = float(pd.to_numeric(b["close"], errors="coerce").iloc[-1])
    return {"ok": True, "bench": bench, "close": close, "ma200": ma200,
            "above_ma200": close > ma200, "dist_ma200": close / ma200 - 1}


def classify_metrics(prior_leg: float | None, days_since_high: int | None,
                     dd: float | None, range_width: float | None,
                     turnover: float | None, float_status: str = "ok") -> tuple[str, list[str]]:
    """纯状态机（无 IO，供单测）：把形态判据翻译成阶段 + 原因。"""
    reasons: list[str] = []
    if prior_leg is None or prior_leg < P["prior_leg_min"]:
        stage = "PRIOR_LEG_MISSING"
        reasons.append(f"前期涨幅 {prior_leg:.2%} < {P['prior_leg_min']:.0%}" if prior_leg is not None else "无前期涨幅")
    elif days_since_high is None or days_since_high < P["days_min"]:
        stage = "JUST_HIGH"
        reasons.append(f"距高点 {days_since_high}d < {P['days_min']}d（还在拉升段）")
    elif dd is not None and dd < P["dd_min"]:
        stage = "BROKEN"
        reasons.append(f"回撤 {dd:.0%} < {P['dd_min']:.0%}（破位，不是横盘）")
    elif days_since_high > P["days_max"]:
        stage = "OVER_3M"
        reasons.append(f"距高点 {days_since_high}d > {P['days_max']}d（出货/蓄势不可分辨）")
    elif range_width is None or range_width > P["range_width_max"]:
        stage = "RANGE_TOO_WIDE"
        reasons.append(f"区间宽度 {range_width:.0%} > {P['range_width_max']:.0%}" if range_width is not None else "区间不可算")
    elif turnover is None:
        stage = "FLOAT_UNAVAILABLE"
        reasons.append(f"流通股本缺失（{float_status}），换手无法验证")
    elif turnover < P["turnover_min"]:
        stage = "TURNOVER_LOW"
        reasons.append(f"累积换手 {turnover:.0%} < {P['turnover_min']:.0%}（洗盘未完成/未验证）")
    else:
        stage = "WASHOUT_WATCH"
        reasons.append(f"形态齐全：拉升{prior_leg:.0%} 回调{dd:.0%} {days_since_high}d 换手{turnover:.0%}")
    return stage, reasons


def analyze(code: str, name: str = "", asof: str | None = None, bench: str = "000906") -> dict:
    """单票筹码/洗盘结构。asof=None 用本地最后一根 bar（并回报该日期）。"""
    out: dict = {"code": code, "name": name, "asof": None, "stage": "NO_DATA",
                 "reasons": [], "params": P}
    if asof is None:
        d = load_bars(code, date.today().isoformat())
        if d is None:
            out["stage"] = "NO_DATA"
            return out
        asof = d["date"].iloc[-1].strftime("%Y-%m-%d")
    d = load_bars(code, asof)
    if d is None or len(d) < 120:
        out["stage"] = "NO_DATA"
        out["reasons"].append(f"bars={0 if d is None else len(d)}<120")
        return out
    out["asof"] = asof
    if d["date"].iloc[-1].strftime("%Y-%m-%d") != asof:
        out["stage"] = "NO_BAR_ASOF"
        out["reasons"].append(f"last_bar={d['date'].iloc[-1].date()} != asof")
        return out

    fs, fs_status = load_float_shares(code, asof)
    d = attach_float(d, fs)
    close = pd.to_numeric(d["close"], errors="coerce")
    vol = pd.to_numeric(d["volume"], errors="coerce")
    out["close"] = float(close.iloc[-1])
    out["float_status"] = fs_status
    if fs is not None and len(fs):
        last_fs = fs["date"].max()
        out["float_last"] = str(last_fs.date())
        out["float_stale_days"] = int((pd.Timestamp(asof) - last_fs).days)

    if _is_fund(code):
        hw = min(P["high_window"], len(d) - 1)
        seg = close.iloc[-hw:]
        hi_pos = len(d) - 1 - int(np.argmax(seg.to_numpy()[::-1]))
        out.update({
            "stage": "ETF_NOT_APPLICABLE", "watch": False,
            "high_close": float(close.iloc[hi_pos]),
            "days_since_high": len(d) - 1 - hi_pos,
            "dd_from_high": float(close.iloc[-1]) / float(close.iloc[hi_pos]) - 1,
            "chip": chip_distribution(d),
            "reasons": ["ETF/基金：无流通股本与'庄家筹码'概念，洗盘框架不适用（只列价格/筹码温度）"],
        })
        return out

    # ── 前期高点与拉升 ────────────────────────────────────────────
    hw = min(P["high_window"], len(d) - 1)
    seg = close.iloc[-hw:]
    hi_pos_rel = int(np.argmax(seg.to_numpy()[::-1]))  # 最近一次最高
    hi_pos = len(d) - 1 - hi_pos_rel
    high_close = float(close.iloc[hi_pos])
    prior_start = max(0, hi_pos - (P["high_window"] - 1))
    prior_low = float(close.iloc[prior_start:hi_pos + 1].min()) if hi_pos > prior_start else float(close.iloc[prior_start])
    prior_leg = high_close / prior_low - 1 if prior_low > 0 else None
    days_since_high = len(d) - 1 - hi_pos
    dd = float(close.iloc[-1]) / high_close - 1 if high_close > 0 else None
    cons = d.iloc[hi_pos:]
    r_hi = float(pd.to_numeric(cons["close"], errors="coerce").max())
    r_lo = float(pd.to_numeric(cons["close"], errors="coerce").min())
    range_width = r_hi / r_lo - 1 if r_lo > 0 else None
    to, to_days = cum_turnover(d, hi_pos, len(d) - 1)
    to60, _ = cum_turnover(d, max(0, len(d) - 60), len(d) - 1)

    # 缩量：近 10 日均量 / 前 40 日均量
    v = vol.to_numpy(float)
    if len(v) >= P["vol_shrink_window"] + P["vol_shrink_ref"]:
        recent = np.nanmean(v[-P["vol_shrink_window"]:])
        base = np.nanmean(v[-(P["vol_shrink_window"] + P["vol_shrink_ref"]):-P["vol_shrink_window"]])
        vol_contraction = float(recent / base) if base and np.isfinite(base) and base > 0 else None
    else:
        vol_contraction = None

    ma10 = float(close.tail(10).mean()); ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    chip = chip_distribution(d)
    reg = bench_regime(bench, asof)

    out.update({
        "prior_leg": prior_leg, "high_close": high_close,
        "days_since_high": days_since_high, "dd_from_high": dd,
        "range_width": range_width, "cum_turnover": to, "turnover_days": to_days,
        "turnover_60d": to60,
        "vol_contraction": vol_contraction, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "chip": chip, "regime": reg,
    })

    # ── 状态机（纯函数，单测覆盖）────────────────────────────────
    stage, reasons = classify_metrics(prior_leg, days_since_high, dd, range_width, to, fs_status)

    watch = stage == "WASHOUT_WATCH" and bool(reg.get("above_ma200"))
    out["stage"] = stage
    out["reasons"] = reasons
    out["watch"] = watch
    out["regime_ok"] = bool(reg.get("above_ma200"))
    if stage == "WASHOUT_WATCH" and not reg.get("above_ma200"):
        out["reasons"].append("大盘在 MA200 下（用户口径：看大盘脸色，不做）")
    if fs is None or fs_status in ("missing", "error"):
        out["reasons"].append(f"流通股本不可得（{fs_status}）——换手类判据不可用")
    if chip:
        out["reasons"].append(
            f"筹码：90%成本带 {chip['width90']:.1%}，获利盘 {chip['winner_rate']:.0%}"
        )
    if vol_contraction is not None:
        tag = "缩量" if vol_contraction < 0.8 else "未缩量"
        out["reasons"].append(f"量能 {vol_contraction:.2f}x（{tag}）")
    out["right_side"] = bool(close.iloc[-1] > ma10 and float(vol.iloc[-1]) > 1.2 * float(vol.tail(20).mean()))
    if out["right_side"]:
        out["reasons"].append("右侧迹象：收盘站上 MA10 且量 > 20 日均量×1.2（仅提示）")
    return out


def _fmt(x, kind="pct"):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    if kind == "pct":
        return f"{x:+.1%}" if x != 0 else "0.0%"
    if kind == "pct0":
        return f"{x:.0%}"
    if kind == "f2":
        return f"{x:.2f}"
    return str(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="逗号分隔代码")
    ap.add_argument("--holdings", default=str(ROOT / "config" / "my_holdings.csv"))
    ap.add_argument("--asof", default=None, help="默认各票本地最后一根 bar")
    ap.add_argument("--bench", default="000906")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    names: dict[str, str] = {}
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        h = pd.read_csv(args.holdings, dtype={"code": str})
        h["code"] = h["code"].str.zfill(6)
        h = h[h["shares"] > 0]
        codes = list(dict.fromkeys(h["code"]))
        names = dict(zip(h["code"], h["name"]))
    if not codes:
        print("无代码")
        return 1

    rows = [analyze(c, names.get(c, ""), asof=args.asof, bench=args.bench) for c in codes]
    print(f"\n筹码/洗盘结构（观察项，不是买卖信号）  asof={args.asof or '各票本地末bar'}")
    print(f"{'代码':<8}{'名称':<8}{'收盘':>8}{'距高':>6}{'回调':>7}{'换手':>7}{'缩量':>6}{'获利盘':>7}{'90%带宽':>8}  状态")
    print("─" * 100)
    for r in rows:
        chip = r.get("chip") or {}
        print(f"{r['code']:<8}{r.get('name',''):<8}{_fmt(r.get('close'),'f2'):>8}"
              f"{str(r.get('days_since_high','—')):>6}{_fmt(r.get('dd_from_high'),'pct'):>7}"
              f"{_fmt(r.get('cum_turnover'),'pct0'):>7}{_fmt(r.get('vol_contraction'),'f2'):>6}"
              f"{_fmt(chip.get('winner_rate'),'pct0'):>7}{_fmt(chip.get('width90'),'pct0'):>8}  "
              f"{r['stage']}{' ✅watch' if r.get('watch') else ''}")
        if r.get("reasons"):
            print(f"         · {'; '.join(r['reasons'])}")
    out = {
        "as_of": args.asof or datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bench": args.bench, "params": P,
        "results": rows,
    }
    p = Path(args.json_out) if args.json_out else LOG_DIR / f"chip_structure_{out['as_of']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nJSON -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
