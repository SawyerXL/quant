"""H1 检验: 宽度 → 前瞻收益/波动(判据见 logs/study_registration_breadth.md)。

主检验 A(方向): ma20_breadth<15% → 未来20日收益, 非重叠样本(n≥10, t>2)
主检验 B(波动): 宽度 → 未来20日已实现波动, |ρ|≥0.2 且分组单调
C: 1d/5d/过热侧=探索不打分。分半稳定性 + SH000906 稳健性强制报告。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent.parent))

BREADTH = Path("/root/quant/logs/breadth_history.csv")
OUT = Path("/root/quant/logs/study_breadth_result.json")


def load_index(code: str) -> pd.Series:
    frames = []
    for yr in range(2018, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/{code}.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    return pd.to_numeric(d.set_index("date")["close"], errors="coerce").sort_index()


def main():
    b = pd.read_csv(BREADTH, parse_dates=["date"]).set_index("date").sort_index()
    out = {}

    def run(index_code: str, tag: str):
        idx = load_index(index_code)
        df = b.join(idx.rename("close"), how="inner").dropna(subset=["ma20_breadth", "close"])
        r1 = df["close"].pct_change()
        fwd20 = df["close"].shift(-20) / df["close"] - 1
        fwd5 = df["close"].shift(-5) / df["close"] - 1
        fwd1 = df["close"].shift(-1) / df["close"] - 1
        fwd_vol20 = r1.shift(-20).rolling(20).std() * np.sqrt(252)  # 未来20日已实现波动
        sig = df["ma20_breadth"] < 0.15

        # A: 非重叠采样
        dates = df.index[sig.fillna(False)]
        samples = []
        last = None
        for dt in dates:
            pos = df.index.get_loc(dt)
            if last is not None and pos - last < 20:
                continue
            samples.append(dt)
            last = pos
        a_rets = fwd20.loc[samples].dropna()
        base = fwd20.dropna()
        a = {"n_episodes": int(len(a_rets)),
             "mean_fwd20": round(float(a_rets.mean()), 4) if len(a_rets) else None,
             "baseline_mean": round(float(base.mean()), 4),
             "t": None, "verdict": "样本不足"}
        if len(a_rets) >= 10:
            t = stats.ttest_1samp(a_rets - base.mean(), 0)
            a["t"] = round(float(t.statistic), 2)
            a["verdict"] = "有证据" if (a_rets.mean() > base.mean() and t.statistic > 2) else "无证据"

        # B: 波动
        bb = pd.DataFrame({"breadth": df["ma20_breadth"], "vol": fwd_vol20}).dropna()
        rho, pval = stats.spearmanr(bb["breadth"], bb["vol"])
        buckets = [(0, .15), (.15, .30), (.30, .70), (.70, .85), (.85, 1.01)]
        bres = []
        for lo, hi in buckets:
            g = bb[(bb["breadth"] >= lo) & (bb["breadth"] < hi)]["vol"]
            bres.append({"range": f"{lo:.0%}-{hi:.0%}", "n": int(len(g)),
                         "mean_vol": round(float(g.mean()), 4) if len(g) else None})
        vols = [x["mean_vol"] for x in bres if x["mean_vol"] is not None]
        mono = all(vols[i] >= vols[i + 1] - 0.02 for i in range(len(vols) - 1))
        b_res = {"spearman": round(float(rho), 3), "p": round(float(pval), 4),
                 "buckets": bres, "monotone_low_to_high": mono,
                 "verdict": "信息存在" if (abs(rho) >= 0.2 and mono) else "无证据"}

        # C 探索
        c = {"n": int(len(df)),
             "fwd1_up_after_low": round(float((fwd1[sig].dropna() > 0).mean()), 4),
             "fwd1_up_base": round(float((fwd1.dropna() > 0).mean()), 4),
             "fwd5_up_after_low": round(float((fwd5[sig].dropna() > 0).mean()), 4),
             "fwd5_up_base": round(float((fwd5.dropna() > 0).mean()), 4)}

        # 分半
        half = df.index < pd.Timestamp("2023-01-01")
        halves = {}
        for name, mask in (("2019-2022", half), ("2023-2026", ~half)):
            s = sig & mask
            dates_h = df.index[s.fillna(False)]
            samp, last = [], None
            for dt in dates_h:
                pos = df.index.get_loc(dt)
                if last is not None and pos - last < 20:
                    continue
                samp.append(dt); last = pos
            rr = fwd20.loc[samp].dropna()
            halves[name] = {"n": int(len(rr)),
                            "mean": round(float(rr.mean()), 4) if len(rr) else None,
                            "base": round(float(fwd20[mask].dropna().mean()), 4)}
        out[tag] = {"n_days": int(len(df)), "A": a, "B": b_res, "C": c,
                    "split_half": halves}
        print(f"\n== {tag} ({index_code}, {len(df)} 日)")
        print(f"A 方向: {a}")
        print(f"B 波动: spearman={b_res['spearman']} monotone={mono} verdict={b_res['verdict']}")
        for x in bres:
            print(f"   {x}")
        print(f"C 探索: {c}")
        print(f"分半: {halves}")

    run("SH000001", "SH000001")
    run("SH000906", "SH000906(稳健性)")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n结果已保存: {OUT}")


if __name__ == "__main__":
    main()
