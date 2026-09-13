"""
单位一致性检查 (2026-09-09 用户定顺序第3步) — 两条断言:
  ①截面绝对量级: 全市场日成交额中位数 ∈ [1e7, 1e9] 元
    = 库口径(万元) [1e3, 1e5] 万元——单峰但整体偏移 10^4 时立即触发
  ②逐股时序连续性: 同票相邻有效日成交额比值 < 100——
    抓 10^4 跳变, 无论全库还是单股(2026-06 切换当天本应报警)
检查最近 N 个交易日全库截面 + 抽样逐股时序; 违规非零退出。
归一完成且每日链跑稳后挂 cron(建议 18:10 日报链前)。
用法: python scripts/unit_consistency_check.py [--days 5]
"""
import sys, argparse
from pathlib import Path
from pathlib import Path as _P

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
import json

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

DAILY_DIR = Path("/data/quant_data_store/daily")


def check_cross_section(dates):
    """断言①: 逐日全市场 amount 中位数 ∈ [1e3, 1e5] 万元"""
    viol = []
    for dt in dates:
        vals = []
        for f in (DAILY_DIR / "2026").glob("*.parquet"):
            if f.stem.startswith(("SH", "SZ")):
                continue
            try:
                d = pd.read_parquet(f, columns=["date", "amount"])
            except Exception:
                continue
            d = d[d["date"].astype(str).str[:10] == dt]
            if d.empty:
                continue
            a = pd.to_numeric(d["amount"], errors="coerce")
            a = a[(a > 0)]
            if len(a):
                vals.append(float(a.median()))
        if not vals:
            continue
        med = float(np.median(vals))
        if not (1e3 <= med <= 1e5):
            viol.append((dt, med))
    return viol


def check_temporal(sample_codes, recent_days, ratio=100.0):
    """断言②: 抽样逐股相邻有效日比值 < 阈值。

    盲区声明(2026-09-12, 闸门第七项): 本断言无法区分 100~2000× 带内的
    "真实市场事件"(妖股退潮 108× 等) 与 "次阈值单位混写"(920689 类
    874× 元日)——100 阈值是保守代理值, 对真实事件的误报由白名单
    (logs/unit_jump_whitelist.json, 逐条文档化理由)显式放行, 不放行
    任何未登记条目。阈值放宽(2000×)已被回归测试否决: 混合态备份上
    100~2000 带含 4 处单位类跳变(605299@06-16 切换日 1183× 等),
    放宽会漏报单位污染。"""
    global RATIO
    RATIO = ratio
    wl_path = Path(__file__).parent.parent / "logs" / "unit_jump_whitelist.json"
    whitelist = set()
    if wl_path.exists():
        for e in json.load(open(wl_path)).get("documented_events", []):
            whitelist.add((e["code"], e["date"]))
    viol = []
    for c in sample_codes:
        d = load_daily(c, "2026-01-01", "2026-12-31")
        if d.empty:
            continue
        d = d.sort_values("date")
        a = pd.to_numeric(d["amount"], errors="coerce")
        prev = None
        prev_date = None
        for i, v in enumerate(a.tolist()):
            if v is None or v <= 0:
                continue
            if prev is not None:
                r = max(v, prev) / min(v, prev)
                if r >= RATIO:
                    dt = str(d["date"].iloc[i])[:10]
                    if (c, dt) not in whitelist:
                        viol.append((c, r))
            prev = v
    return viol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--ratio", type=float, default=100.0,
                    help="时序跳变阈值(2026-09-12 规格讨论: 100为过紧代理值, 2000候选)")
    args = ap.parse_args()
    # 最近 N 个交易日(上证指数日历)
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    dates = cal[-args.days:]
    print(f"检查 {len(dates)} 个交易日截面 + 时序抽样")

    v1 = check_cross_section(dates)
    print(f"断言①截面量级: {'✓' if not v1 else '✗'} "
          + (f"{v1}" if v1 else ""))
    meta = load_meta("stock_info_full")
    bl = load_blacklist()
    if "code" in meta.columns:
        codes = [str(c).zfill(6) for c in meta["code"].tolist()
                 if str(c).zfill(6) not in bl]
    else:
        # 备份目录无 meta 时从 daily 文件名取宇宙(回归测试场景)
        codes = sorted({f.stem for f in (DAILY_DIR / "2026").glob("*.parquet")
                        if not f.stem.startswith(("SH", "SZ"))})
    import random
    random.seed(11)
    sample = []
    for c in random.sample(codes, 200):
        if c.startswith(("920", "430", "83", "87")):
            continue  # 缓处理票除外
        d = load_daily(c, "2026-01-01", "2026-12-31")
        if d.empty:
            continue
        med = float(pd.to_numeric(d["amount"], errors="coerce").median())
        if med < 1000:
            continue  # 退化文件除外
        sample.append(c)
    v2 = check_temporal(sample, dates, args.ratio)
    print(f"断言②时序连续性(抽样200只): {'✓' if not v2 else '✗'} "
          + (f"{len(v2)}处跳变, 示例{v2[:5]}" if v2 else ""))
    # 断言③(2026-09-12 加): 退化文件监控——amount 中位数<1000 的文件
    # 应全部已在隔离清单; 新出现的=源端持续产生伪零额(根因未查清,
    # 静默增长比污染本身危险)
    from pathlib import Path as _P
    uq_path = _P(__file__).parent.parent / "data_store" / "meta" / "unit_quarantine.json"
    uq_codes = set()
    if uq_path.exists():
        try:
            uq_codes = {str(x["code"]).zfill(6)
                        for x in json.load(open(uq_path))}
        except Exception:
            pass
    new_deg = []
    for f in sorted((DAILY_DIR / "2026").glob("*.parquet")):
        if f.stem.startswith(("SH", "SZ")):
            continue
        if f.stem in uq_codes:
            continue
        try:
            d = pd.read_parquet(f, columns=["amount"])
            med = float(pd.to_numeric(d["amount"], errors="coerce").median())
            if med < 1000:
                new_deg.append((f.stem, round(med, 1)))
        except Exception:
            continue
    print(f"断言③退化文件监控: {'✓ 无新增' if not new_deg else '✗ ' + str(new_deg[:5])}")

    # 断言④(2026-09-12 加): 尾部上限——五道审计全检验"分布中心/内部
    # 一致性", 而 pool30 只消费尾部(13只元幽灵1.68e8霸榜时中位数审计
    # 全绿)。绝对物理上限: A股单票单日成交额极值~400亿, 1000亿留2.5倍
    # 余量; 相对: top1/门槛 < 50(健康值~10倍内)
    cap_abs = 1e7  # 万元 = 1000亿元
    tail_bad = []
    top1 = 0.0
    thr60 = 0.0
    for f in sorted((DAILY_DIR / "2026").glob("*.parquet")):
        if f.stem.startswith(("SH", "SZ")):
            continue
        if f.stem in uq_codes or f.stem.startswith(("920", "430", "83", "87")):
            continue  # 缓处理票不参与尾部审计
        try:
            d = pd.read_parquet(f, columns=["date", "amount"])
        except Exception:
            continue
        a = pd.to_numeric(d["amount"], errors="coerce")
        a = a[a > 0]
        if len(a) == 0:
            continue
        mx = float(a.max())
        # 688825@07-27(1411亿)保持 RED 待裁: 三项独立判别中 ①市场占比
        # 0.13%<5% → 正常 ②分布距 p99.99(98.5亿) 14× → 疑似孤立点
        # ③换手率/事件无数据——白名单不落地(用户 2026-09-13 纪律)
        if mx > cap_abs:
            tail_bad.append((f.stem, round(mx, 0)))
        top1 = max(top1, mx)
        if f.stem not in uq_codes and str(d["date"].max())[:10] >= "2026-09-01":
            thr60 = max(thr60, float(np.median(a.tail(20))) if len(a) >= 10 else 0.0)
    rel_ok = (thr60 > 0 and top1 / thr60 < 50)
    print(f"断言④尾部上限: 绝对 {'✓' if not tail_bad else '✗ ' + str(tail_bad[:5])}"
          f" | 相对 top1={top1:.0f}/门槛60={thr60:.0f} 比 {top1/thr60 if thr60 else 0:.1f} "
          f"{'✓' if rel_ok else '✗'}")
    # 断言⑤(2026-09-13 过度归一修复后): 有交易日成交额<5万元的票=异常
    # (检测体系此前全部单向——只抓"归一不足", 过度归一方向无审计;
    # 17只新票被日期规则二次除到0.4~3.5万元全靠 MCP 抽验才暴露)
    over = []
    for f in sorted((DAILY_DIR / "2026").glob("*.parquet")):
        if f.stem.startswith(("SH", "SZ")):
            continue
        if f.stem in uq_codes or f.stem.startswith(("920", "430", "83", "87")):
            continue
        try:
            d = pd.read_parquet(f, columns=["date", "amount", "close"])
        except Exception:
            continue
        thin = 0
        for _, r in d.iterrows():
            a = pd.to_numeric(r["amount"], errors="coerce")
            cl = pd.to_numeric(r["close"], errors="coerce")
            if pd.notna(a) and 0 < a < 5 and pd.notna(cl) and cl > 0:
                thin += 1
        if thin:
            over.append((f.stem, thin))
    print(f"断言⑤过度归一方向: {'✓' if not over else '✗ ' + str(over[:5])}")
    # 断言⑦(2026-09-13): 全市场聚合量——不依赖单票正确性/分布形状/
    # 阈值调参, 几十只元口径票就会让总和越界(9/7 若有此断言单位混写
    # 当天即暴露; 2026-09-13 的 110万亿分母正是被隔离区元票撑爆)
    # 断言⑦(2026-09-13): 全市场聚合量——**全库口径**(含隔离区):
    # 隔离区会吸收它自己造成的证据(幂等盲区同型), 排除口径的断言
    # 看不见"几十只票被判异常进隔离→总额恢复"这一路径。
    # 主判据 = 全库总额 ∈ [3000亿, 3万亿]元; 参考 = 排除隔离后总额;
    # 两者比值 > 3 即告警(2026-09-13 当日实测 53 倍——110万亿 vs 2.06万亿)
    totals_all = {}
    totals_ex = {}
    for f in sorted((DAILY_DIR / "2026").glob("*.parquet")):
        if f.stem.startswith(("SH", "SZ")):
            continue
        try:
            d = pd.read_parquet(f, columns=["date", "amount"])
        except Exception:
            continue
        d = d[d["date"].astype(str).str[:10].isin(dates)]
        if d.empty:
            continue
        for dt2, grp in d.groupby(d["date"].astype(str).str[:10]):
            s = float(pd.to_numeric(grp["amount"], errors="coerce")[
                pd.to_numeric(grp["amount"], errors="coerce") > 0].sum())
            totals_all[dt2] = totals_all.get(dt2, 0.0) + s
            if f.stem not in uq_codes and not f.stem.startswith(
                    ("920", "430", "83", "87")):
                totals_ex[dt2] = totals_ex.get(dt2, 0.0) + s
    agg_bad = [(dt2, round(v / 1e8, 2)) for dt2, v in totals_all.items()
               if not (3e7 <= v <= 3e8)]
    ratios = [(dt2, round(totals_all[dt2] / totals_ex.get(dt2, 1), 1))
              for dt2 in totals_all]
    ratio_bad = [x for x in ratios if x[1] > 3]
    print(f"断言⑦聚合量(全库总额∈[3000亿,3万亿]元): "
          f"{'✓' if not agg_bad else '✗ ' + str(agg_bad[:5])}")
    print(f"  全库/排除隔离 比值: {[x for x in ratios]} "
          f"{'⚠️>3' if ratio_bad else '✓'}")
    import json as _j
    _j.dump({"totals_all": totals_all, "totals_ex": totals_ex},
            open("logs/daily_total_curve.json", "w"))
    if v1 or v2 or new_deg or tail_bad or not rel_ok or over or agg_bad \
            or ratio_bad:
        print("⚠️ 单位一致性违规")
        sys.exit(1)
    print("单位一致性通过")


if __name__ == "__main__":
    main()
