"""下修史标记 PIT 框架 (2026-10-01 建, 任务#20, 最低优先级; 数据样本=20只券)。

立项(用户 09-29): 下修是发行人行为信号, 30% 持仓有下修史而策略无感知。
测法=下修史横截面标记分桶月收益(有/无、单次/多次)。
**陷阱先锁(用户): 事件必须按 change_date PIT 应用——禁"是否曾下修"现值
状态(未来信息, V2 bug 同型); 符号不预设(力保转股=正面 vs 基本面恶化=负面),
判据先锁; 价值=风险标记非 alpha; 非 v2 后门(标记有用≠重开 premium)。**

本框架职责:
  1. 事件表载入(缓存 parquet: code/change_date/bef/aft; 节后全量摄取=独立脚本)
  2. PIT 标记: 在快照日 D, 券的下修状态 = 所有 change_date ≤ D 的事件中
     (bef−aft)/bef > 5% 的次数(0/1/2+)
  3. 分桶: 月度调仓所选券按标记分桶, 统计桶级月收益(mean/N/t 双侧)
用法: python scripts/cb_xiuxiu_marker.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

EVENTS_FILE = Path("logs/cb_convert_price_events.parquet")
# 单次转股价降幅>5% 判为下修——**仅对 JSL 权威下修表成立**。G2 全事件表
# 含送转/分红机械调整(10转4 即 -28.6%), 用本阈值会误计(2026-10-21 实测
# 312 条 G2 大降仅 196 条是 JSL 下修); 权威口径见 pull_cb_xiuxiu_jsl.py。
XIUXIU_THRESH = 0.05


def load_events(path=None) -> pd.DataFrame:
    """path 可换权威下修史（2026-10-21: JSL 下修记录同构表；G2 事件含
    送转/分红调整，>5% 阈值会误计——见 pull_cb_xiuxiu_jsl.py）。"""
    f = Path(path) if path else EVENTS_FILE
    if not f.exists():
        return pd.DataFrame(columns=["code", "change_date", "bef", "aft"])
    df = pd.read_parquet(f)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["change_date"] = pd.to_datetime(df["change_date"])
    return df


def pit_mark(df: pd.DataFrame, code: str, snap_date: pd.Timestamp) -> int:
    """PIT 下修次数: 只统计 change_date ≤ snap_date 的事件(陷阱防护核心——
    未来事件不可见)。返回 0 / 1 / 2+(封顶 2)。"""
    sub = df[(df["code"] == code) & (df["change_date"] <= snap_date)]
    if sub.empty:
        return 0
    sub = sub[sub["bef"].notna() & (sub["bef"] > 0)]
    n_xiu = int(((sub["bef"] - sub["aft"]) / sub["bef"] > XIUXIU_THRESH).sum())
    return min(n_xiu, 2)


def bucket_returns(selection_returns: list[tuple]) -> dict:
    """selection_returns: [(bucket, month_ret), ...] → 分桶统计。
    判读模板(执行前锁): 双侧 t 检验, 不预设符号; 剂量响应(0→1→2+)另看;
    最小桶样本 ≥ 50 券-月。"""
    out = {}
    df = pd.DataFrame(selection_returns, columns=["bucket", "ret"])
    for b in sorted(df["bucket"].unique()):
        r = df[df["bucket"] == b]["ret"].dropna()
        if len(r) < 5:
            out[b] = {"N": len(r), "mean": float("nan"), "t": float("nan")}
            continue
        t = r.mean() / (r.std() / np.sqrt(len(r))) if r.std() > 0 else 0.0
        out[b] = {"N": len(r), "mean": float(r.mean()), "t": float(t)}
    return out


def main():
    events = load_events()
    print(f"事件表: {len(events)} 行, {events['code'].nunique()} 只券")
    if events.empty:
        print("(无事件数据——节后全量摄取后执行分桶; 本框架此刻只验 PIT 逻辑)")
        return
    # 演示: 三只已知下修史券在 2025-01-01 与 2026-09-01 两个快照日的 PIT 状态
    for code in ["110081", "127061", "127027"]:
        for d in [pd.Timestamp("2023-01-01"), pd.Timestamp("2025-01-01"),
                  pd.Timestamp("2026-09-01")]:
            print(f"  {code}@{d.date()}: 下修次数(PIT) = {pit_mark(events, code, d)}")
    print("\n判读模板(执行前由用户锁死): 双侧 t / 不预设符号 / 剂量响应 / N≥50 券-月")


if __name__ == "__main__":
    main()
