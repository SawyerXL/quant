"""期货吃贴水腿实验 N=44（2026-10-11 登记, 判据已锁）。

两阶段:
  --mde-only: 拉四合约主连+四现货指数, 算月频基差σ→TE→SE→MDE(2×SE),
              vs 可信效应量 3~5%/年(锁死) → 打印可判读性结论
  (默认)全量: 臂A=现金管理基准(511360 +1.43%/年) vs 臂B=各合约滚动多头+
              现金管理; 按已锁判据出 α/正交性/成本敏感性/分段分合约

口径: 主连 close-to-close 滚动收益 − 现货指数收益 = 基差 P&L(价格口径,
双边一致); 移仓=主连自身(自动滚动近似), 成本=万0.23×2+滑点0.5tick/月;
保证金15%+余现金1.43%/年。判据(锁): ①α≥MDE且MDE先行 ②正交性|r|<0.3
(vs 组合两腿, 读 logs/stock_cb_joint_returns.csv) ③移仓成本±50% α符号
不变 ④分合约分段报告(衰减趋势=结论一部分)。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))

FUT = {"IC0": "sh000905", "IM0": "sh000852", "IF0": "sh000300", "IH0": "sh000016"}
CASH_Y = 0.0143  # 511360 年化(已测机器值)
COST_RT = 0.000023 * 2  # 手续费双边
SLIP = 0.2  # 0.5 tick ≈ 0.2 点(IC/IM 最小变动0.2点)


def load_fut(sym):
    df = ak.futures_zh_daily_sina(symbol=sym)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].astype(float)


def load_spot(sym):
    df = ak.stock_zh_index_daily(symbol=sym)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].astype(float)


def mde_stage():
    print("=== 阶段1: MDE 先行 (判据①前置) ===")
    rows = []
    for fsym, ssym in FUT.items():
        try:
            f = load_fut(fsym)
            s = load_spot(ssym)
        except Exception as e:
            print(f"{fsym}: 数据拉取失败 {str(e)[:60]}")
            continue
        j = pd.concat([f.rename("F"), s.rename("S")], axis=1, join="inner").dropna()
        # 月频非重叠: 每月末取点, 计算 期货收益-现货收益 的对数差分
        m = j.resample("ME").last().dropna()
        r = np.log(m["F"]).diff() - np.log(m["S"]).diff()
        r = r.dropna()
        years = (m.index[-1] - m.index[0]).days / 365.25
        sig = r.std(ddof=1)
        te = sig * np.sqrt(12)
        se = te / np.sqrt(years)
        mde = 2 * se
        ok = mde <= 0.05
        rows.append((fsym, years, sig, te, se, mde, ok))
        print(f"{fsym}: n={len(r)}月 T={years:.1f}年 月基差σ={sig*100:.2f}% "
              f"TE={te*100:.2f}% SE={se*100:.2f}pp MDE(2×SE)={mde*100:.2f}pp "
              f"→ {'≤5% 可判读 ✓' if ok else '>5% 不可判读 ✗'}")
    if not rows:
        return False
    # 可信效应量带 3~5%/年(锁): MDE ≤ 5%(带上沿) 即跑, >5% 不跑
    run = all(ok for _, _, _, _, _, _, ok in rows)
    print(f"\n结论: {'全部合约 MDE ≤5% → 可跑全量' if run else '存在 MDE >5% 合约 → 该合约不跑(其余按各自 MDE)'}")
    return run


def third_friday_roll_dates(start="2017-01", end="2026-11"):
    """每月 3rd Friday − 3 个交易日(近似到期前滚动, 注册口径=到期前3日)。"""
    from data.storage import load_meta
    cal = load_meta("trade_calendar")["trade_date"].astype(str).tolist()
    out = {}
    cur = pd.Period(start, freq="M")
    endp = pd.Period(end, freq="M")
    while cur <= endp:
        # 当月第三个周五
        first = pd.Timestamp(cur.strftime("%Y-%m-01"))
        friday1 = first + pd.offsets.Week(weekday=4)
        third = friday1 + pd.offsets.Week(2)
        d = third.strftime("%Y-%m-%d")
        if d in cal:
            i = cal.index(d)
            out[cur.strftime("%Y-%m")] = cal[max(0, i - 3)]
        else:
            v = [x for x in cal if x <= d]
            out[cur.strftime("%Y-%m")] = v[-3] if len(v) >= 3 else (v[-1] if v else d)
        cur += 1
    return out


def load_contract_splice(idx, start, end, cache_dir="logs/fut_contracts"):
    """当月合约滚动拼接: 持仓合约=交割月当月(roll日前)或次月(roll日后)。"""
    import time
    from pathlib import Path
    cache = Path(cache_dir)
    cache.mkdir(exist_ok=True)
    roll_dates = third_friday_roll_dates()
    # 需要覆盖的合约月份: start月 到 end月+1
    months = pd.period_range(start, end, freq="M").tolist()
    months.append(months[-1] + 1)
    series = {}
    for m in months:
        code = f"{idx}{m.strftime('%y%m')}"
        fpath = cache / f"{code}.csv"
        if not fpath.exists():
            try:
                df = ak.futures_zh_daily_sina(symbol=code)
                df.to_csv(fpath, index=False)
                time.sleep(0.15)
            except Exception as e:
                print(f"  {code}: 拉取失败 {str(e)[:40]}")
                continue
        df = pd.read_csv(fpath)
        df["date"] = pd.to_datetime(df["date"])
        series[code] = df.set_index("date")["close"].astype(float)
    # 拼接: 逐日选合约(缺合约月份留空, 不伪造)
    all_dates = sorted(set().union(*[set(s.index) for s in series.values()]))
    front = []
    for d in all_dates:
        ds = d.strftime("%Y-%m-%d")
        code_cur = f"{idx}{d.strftime('%y%m')}"
        roll_d = roll_dates.get(d.strftime("%Y-%m"), "2099-12-31")
        nm = pd.Timestamp(d) + pd.offsets.MonthEnd(1)
        code_next = f"{idx}{nm.strftime('%y%m')}"
        if ds < roll_d:
            pick = code_cur if code_cur in series else code_next
        else:
            pick = code_next if code_next in series else code_cur
        if pick in series:
            front.append((d, pick))
    out = {}
    for d, c in front:
        if d in series[c].index:
            out[d] = series[c].loc[d]
    return pd.Series(out).sort_index()


def full_stage():
    print("\n=== 阶段2: 全量臂 (判据①②③④) ===")
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
    verdicts = []
    for fsym, ssym in FUT.items():
        f = load_fut(fsym)
        s = load_spot(ssym)
        j = pd.concat([f.rename("F"), s.rename("S")], axis=1, join="inner").dropna()
        # 日频基差 P&L(期货收益-现货收益), 月频非重叠
        d_ret = np.log(j["F"]).diff() - np.log(j["S"]).diff()
        m_ret = (np.log(j["F"]).diff() - np.log(j["S"]).diff()).resample("ME").sum()
        m_ret = m_ret.dropna()
        years = (j.index[-1] - j.index[0]).days / 365.25
        alpha = m_ret.mean() * 12
        se = (m_ret.std(ddof=1) * np.sqrt(12)) / np.sqrt(years)
        # 成本: 月度移仓 万0.23×2 + 0.5tick(≈0.2点, 按均价折算)
        avg_f = j["F"].mean()
        cost_monthly = 0.000023 * 2 + 0.2 / avg_f
        cost_yr = cost_monthly * 12
        for scale, tag in [(1.0, "基准"), (0.5, "−50%"), (1.5, "+50%")]:
            a_net = alpha - cost_yr * scale
            print(f"{fsym} 成本{tag}: α_net={a_net*100:.2f}%/年 ±{se*100:.2f}pp "
                  f"(毛α={alpha*100:.2f}%, 成本={cost_yr*100:.2f}%/年)")
        # 判据① α ≥ MDE(2×SE)
        mde = 2 * se
        c1 = (alpha - cost_yr) >= mde
        # 判据③ 成本敏感性: ±50% 下 α_net 符号不变
        c3 = ((alpha - cost_yr * 0.5) > 0) and ((alpha - cost_yr * 1.5) > 0)
        # 判据② 正交性: 日基差 P&L vs 组合两腿
        jj = pd.concat([d_ret.rename("basis"), joint["s"], joint["cb"]],
                       axis=1, join="inner").dropna()
        r_s = jj["basis"].corr(jj["s"])
        r_cb = jj["basis"].corr(jj["cb"])
        c2 = (abs(r_s) < 0.3) and (abs(r_cb) < 0.3)
        # 判据④ 分段(IC/IF/IH: 2017-20/2021-24/2025-26; IM: 2022-24/2025-26)
        if fsym == "IM0":
            segs = [("2022-2024", "2022-01-01", "2025-01-01"),
                    ("2025-2026", "2025-01-01", "2030-01-01")]
        else:
            segs = [("2017-2020", "2017-01-01", "2021-01-01"),
                    ("2021-2024", "2021-01-01", "2025-01-01"),
                    ("2025-2026", "2025-01-01", "2030-01-01")]
        seg_out = []
        for name, a, b in segs:
            w = m_ret[(m_ret.index >= a) & (m_ret.index < b)]
            if len(w):
                seg_out.append(f"{name}: {(w.mean()*12)*100:+.2f}%/年(n={len(w)})")
        print(f"  {fsym} 分段α: " + " | ".join(seg_out))
        print(f"  {fsym} 正交性: vs 股票腿 r={r_s:.3f}, vs CB腿 r={r_cb:.3f} → {'✓' if c2 else '✗'}")
        print(f"  {fsym} 判据: ①α≥MDE {'✓' if c1 else '✗'} ②正交 {'✓' if c2 else '✗'} "
              f"③成本敏感 {'✓' if c3 else '✗'}")
        verdicts.append((fsym, alpha, cost_yr, se, c1, c2, c3))
    print("\n=== 判读(机器值) ===")
    for fsym, alpha, cost, se, c1, c2, c3 in verdicts:
        print(f"{fsym}: 净α={(alpha-cost)*100:+.2f}%/年±{se*100:.2f}pp "
              f"→ {['全部通过→候选', '存在✗→逐条看'][int(not (c1 and c2 and c3))]}")
    stage3()


def stage3():
    """合约级滚动拼接复测(主连把移仓跳空吃掉了——近月续月的真实 P&L 需拼接)。"""
    print("\n=== 阶段3: 合约级滚动拼接 (IC/IM, 主连滚跳伪影修正) ===")
    for idx, ssym, start, end in [("IC", "sh000905", "2017-01", "2026-11"),
                                  ("IM", "sh000852", "2022-08", "2026-11")]:
        f = load_contract_splice(idx, start, end)
        s = ak.stock_zh_index_daily(symbol=ssym)
        s["date"] = pd.to_datetime(s["date"])
        s = s.set_index("date")["close"].astype(float)
        j = pd.concat([f.rename("F"), s.rename("S")], axis=1, join="inner").dropna()
        m_ret = (np.log(j["F"]).diff() - np.log(j["S"]).diff()).resample("ME").sum().dropna()
        years = (j.index[-1] - j.index[0]).days / 365.25
        alpha = m_ret.mean() * 12
        se = m_ret.std(ddof=1) * np.sqrt(12) / np.sqrt(years)
        avg_f = j["F"].mean()
        cost_yr = (0.000023 * 2 + 0.2 / avg_f) * 12
        # 正交性
        d_ret = np.log(j["F"]).diff() - np.log(j["S"]).diff()
        joint = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
        jj = pd.concat([d_ret.rename("basis"), joint["s"], joint["cb"]],
                       axis=1, join="inner").dropna()
        r_s, r_cb = jj["basis"].corr(jj["s"]), jj["basis"].corr(jj["cb"])
        # 分段
        if idx == "IM":
            segs = [("2022-2024", "2022-01-01", "2025-01-01"),
                    ("2025-2026", "2025-01-01", "2030-01-01")]
        else:
            segs = [("2017-2020", "2017-01-01", "2021-01-01"),
                    ("2021-2024", "2021-01-01", "2025-01-01"),
                    ("2025-2026", "2025-01-01", "2030-01-01")]
        seg_out = []
        for name, a, b in segs:
            w = m_ret[(m_ret.index >= a) & (m_ret.index < b)]
            if len(w):
                seg_out.append(f"{name}: {(w.mean()*12)*100:+.2f}%/年(n={len(w)})")
        net = (alpha - cost_yr) * 100
        c1 = net >= 2 * se * 100
        c3 = (alpha - cost_yr * 0.5) > 0 and (alpha - cost_yr * 1.5) > 0
        c2 = abs(r_s) < 0.3 and abs(r_cb) < 0.3
        print(f"{idx}(拼接): 净α={net:+.2f}%/年±{se*100:.2f}pp 毛α={alpha*100:+.2f}% "
              f"成本={cost_yr*100:.2f}%/年")
        print(f"  分段: " + " | ".join(seg_out))
        print(f"  正交: vs股票 r={r_s:.3f} vsCB r={r_cb:.3f} {'✓' if c2 else '✗'}")
        print(f"  判据: ①α≥MDE {'✓' if c1 else '✗'} ②正交 {'✓' if c2 else '✗'} "
              f"③成本敏感 {'✓' if c3 else '✗'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mde-only", action="store_true")
    args = ap.parse_args()
    if args.mde_only:
        mde_stage()
        return
    mde_stage()
    full_stage()


if __name__ == "__main__":
    main()
