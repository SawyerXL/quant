"""
官方指数成分归档（2026-09-16，用户裁定"实盘宇宙与回测宇宙解耦"的止血件）。

背景：官方成分静态 xls 是覆盖式更新——每次官方样本调整，上一期成分
永久消失。不归档，半年后再讨论依然只有"当期"。本脚本每周下载当期
官方成分，**只增不覆盖**落盘，as_of 进元数据：
  data_store/meta/members_history/000906_<as_of>.parquet

回测宇宙的 PIT 无源 → 维持截断表+声明（不变）；实盘宇宙只需要
"今天的成分" → 官方当期完全够用，不制造前视。二者解耦的物理形式
就是这份只增归档：当期表既供实盘读取，也逐年积累成未来的 PIT。

同时写当期官方表 data_store/meta/csi800_members_official.parquet
（实盘 800 腿的读取目标；元数据含 as_of）。

用法: python scripts/archive_index_members.py
cron: 每周一 07:30（官方调整在 6/12 月，周频覆盖且当天归档止血）
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import requests
import pandas as pd
from io import BytesIO

from data.storage import DATA_STORE

URL = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
       "file/autofile/cons/{code}cons.xls")
HIST_DIR = Path(DATA_STORE) / "meta" / "members_history"
OFFICIAL = Path(DATA_STORE) / "meta" / "csi800_members_official.parquet"


def fetch_members(code: str) -> pd.DataFrame:
    import time
    last = None
    for attempt in range(3):
        try:
            r = requests.get(URL.format(code=code), timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            df = pd.read_excel(BytesIO(r.content))
            df.columns = [str(c).split(" ")[0] for c in df.columns]
            return df
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise last


def main():
    as_of = date.today().isoformat()
    df906 = fetch_members("000906")          # 中证800 官方当期
    code_col = [c for c in df906.columns if "成份券" in c and "代码" in c][0]
    codes = sorted({str(c).zfill(6) for c in df906[code_col]})
    out = pd.DataFrame({"code": codes, "as_of": as_of})
    if len(codes) != 800:
        raise ValueError(f"官方当期成分数 {len(codes)} ≠ 800, 拒绝归档"
                         f"(源可能改版或异常)")
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(HIST_DIR / f"000906_{as_of}.parquet", index=False)
    out.to_parquet(OFFICIAL, index=False)
    print(f"归档: 000906_{as_of}.parquet ({len(codes)} 只) + 当期官方表已更新")


if __name__ == "__main__":
    main()
