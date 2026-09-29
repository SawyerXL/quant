"""CB 选股过滤唯一实现（闸门二十一，2026-09-22 立）。

cb_signal 与所有回测脚本共用本函数——任何一侧新增过滤条件一律改这里，
禁止各脚本自带一份拷贝。背景（2026-09-22 实测双向失配）：
- 回测有 404 剔除、实盘信号也有——但三份回测脚本各有拷贝且漏掉 404，
  404004（评级 A 逃过过滤）连续 22 个月霸占 TOP25，回测与实盘不同构
  （回测低估方向，−0.042 夏普）。
- 反向：cb_signal 声称"与回测口径一致"但缺评级/规模/强赎三道过滤，
  实盘持有 BBB-（110081）与 CC（118027）——回测永远不会选的券
  （实盘风险高于回测模型方向）。

口径（回测 spec 定案）：
① 404 退市板块债剔除（违约债，实盘流动性买不进；dblow 霸榜）
② 评级 A- 及以上
③ 规模 ≥ 0.5 亿
④ 临近强赎排除（价 > 130 且溢价 < 5%）
⑤ 双低排序 dblow（快照已含则直接用；缺失时 price + 100×premium/100 现算；
   口径注 2026-09-29 用户定案: 快照溢价率恒 0 → dblow=price 单因子,
   price-only 是定案非欠账——修复否决三条: 持仓30%下修率(v1历史段算错)/
   偏误方向乐观(伪alpha)/股票链耦合, 详见账本 09-29 节）
"""
import pandas as pd


def filter_universe(df: pd.DataFrame) -> pd.DataFrame:
    """应用①②③④过滤，返回过滤后的副本。"""
    df = df.copy()
    df = df[~df["code"].astype(str).str.startswith("404")]
    rating_order = {"AAA": 0, "AA+": 1, "AA": 2, "AA-": 3, "A+": 4,
                    "A": 5, "A-": 6}
    df["rn"] = df["rating"].map(rating_order).fillna(99)
    df = df[df["rn"] <= 6]
    if "size" in df.columns:
        df = df[df["size"] >= 0.5]
    if "price" in df.columns and "premium" in df.columns:
        df = df[~((df["price"] > 130) & (df["premium"] < 5))]
    return df


def select_double_low(snap: pd.DataFrame, top=None) -> list:
    """双低排序取前 top 只（top=None 返回全排名，供停牌过滤向下补足用）。"""
    df = filter_universe(snap)
    if "dblow" not in df.columns:
        df["dblow"] = df["price"] + 100 * df["premium"] / 100
    df["dblow"] = pd.to_numeric(df["dblow"], errors="coerce")
    df = df.dropna(subset=["dblow"])
    ranked = df.sort_values("dblow")["code"].tolist()
    return ranked[:top] if top else ranked
