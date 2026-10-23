"""H4（N=76）：降准 → 未来 6/12 个月收益（预注册 logs/prereg_regime_20261023.md）。

口径（保守、无前视）：
- 事件=存款准备金率**下调**（`macro_china_reserve_requirement_ratio` 的
  生效时间，调整后<调整前；公告早于生效日，用生效日之后的首个交易日收盘
  作为 t0，反而更保守）。
- 目标：t0→+6M/+12M 的上证收盘收益（日历月，取最近交易日）。
- 事件级 t = mean/(sd/√n)；IS ≤2018-12，OOS ≥2019-01。
- MDE = 2.8×sd_event/√n（事件级；只作量级参考）。
- 判据：PASS = IS 均值>0且t≥2.5、OOS 同号t≥1.5、|OOS|≥MDE；否则按矩阵。
- 降息为次要（诊断）；印花税/IPO 暂停事件太少，不检验。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_daily

ROOT = Path(__file__).parent.parent
OUT = ROOT / "logs" / "regime_h4_20261023.json"


def _events():
    import akshare as ak
    rrr = ak.macro_china_reserve_requirement_ratio()
    ev = []
    for _, r in rrr.iterrows():
        try:
            d = pd.Timestamp(str(r["生效时间"]).replace("年", "-")
                             .replace("月", "-").replace("日", ""))
            before = float(str(r["大型金融机构-调整前"]).replace("%", ""))
            after = float(str(r["大型金融机构-调整后"]).replace("%", ""))
            if after < before:
                ev.append(d)
        except Exception:
            continue
    return sorted(set(ev))


def _fwd(sh: pd.Series, d: pd.Timestamp, months: int):
    idx = sh.index.searchsorted(d, side="right")  # 生效日之后的首个交易日
    if idx >= len(sh):
        return np.nan
    t0 = sh.index[idx]
    end = t0 + pd.DateOffset(months=months)
    j = sh.index.searchsorted(end, side="left")
    if j >= len(sh):
        return np.nan
    return float(sh.iloc[j] / sh.iloc[idx] - 1)


def _stat(vals):
    v = pd.Series(vals).dropna().astype(float)
    if len(v) < 3:
        return {"n": len(v), "mean": None, "t": None, "sd": None, "mde": None}
    sd = float(v.std(ddof=1)); n = len(v)
    return {"n": n, "mean": float(v.mean()),
            "t": float(v.mean() / (sd / np.sqrt(n))),
            "sd": sd, "mde": float(2.8 * sd / np.sqrt(n))}


def main():
    ev = _events()
    sh = load_daily("SH000001", "2007-01-01", "2026-12-31")
    sh["date"] = pd.to_datetime(sh["date"])
    sh = sh.set_index("date")["close"].astype(float).sort_index()
    rows = []
    for d in ev:
        rows.append({"date": str(d)[:10],
                     "r6": _fwd(sh, d, 6), "r12": _fwd(sh, d, 12)})
    df = pd.DataFrame(rows).dropna(subset=["r6"])
    is_df = df[df["date"] <= "2018-12-31"]
    oos_df = df[df["date"] >= "2019-01-01"]
    out = {"n": 76, "hypothesis": "H4 RRR cut → 6/12M",
           "prereg": "logs/prereg_regime_20261023.md",
           "n_events": len(df), "events": df["date"].tolist(),
           "is": {"n": len(is_df), "r6": _stat(is_df["r6"]),
                  "r12": _stat(is_df["r12"])},
           "oos": {"n": len(oos_df), "r6": _stat(oos_df["r6"]),
                   "r12": _stat(oos_df["r12"])},
           "as_of": datetime.now().isoformat(timespec="seconds")}
    # 主判据用 6M
    is6, oos6 = out["is"]["r6"], out["oos"]["r6"]
    if (is6["mean"] is None or oos6["mean"] is None or oos6["n"] < 5):
        verdict = "UNDERPOWERED"
    elif is6["mean"] <= 0 or oos6["mean"] <= 0:
        verdict = "FAIL"
    elif (is6["t"] >= 2.5 and oos6["t"] >= 1.5
          and abs(oos6["mean"]) >= (oos6["mde"] or 0)):
        verdict = "PASS"
    elif is6["t"] >= 1.5:
        verdict = "SUGGESTIVE"
    else:
        verdict = "UNDERPOWERED"
    out["verdict_6m"] = verdict
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
