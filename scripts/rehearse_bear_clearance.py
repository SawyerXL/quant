"""
9/7 bear 清仓排练（2026-09-06，新引擎语义首次实盘前的 paper 模式全链路验证）。

做法: 用实盘持仓快照构造 bear 信号(regime=bear, sell=全部持仓), 用
MockQMTClient 喂给 Trader._execute_bear 完整跑一遍下单逻辑, 验证:
  ① diff 对全部持仓生成全额卖单、在途净额修正在全清场景不重复下单
  ② 单笔10万分笔: 标记任何单票市值>10万需要拆单的情况
  ③ 卖出定价v3(实时价×0.98)tick归一后全部合法(无>2位小数价格)
  ④ 跌停禁卖: 标记任何现价距跌停<3%的持仓(周一可能一字跌停需排队)
不产生任何真实订单。
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
logger.remove()

from execution.trader import Trader, _limit_prices, _build_st_map


def main():
    # 实盘持仓快照
    snap = json.loads(Path("logs/qmt_positions_latest.json").read_text(encoding="utf-8"))
    pos = snap.get("positions", {})
    print(f"实盘快照: {snap.get('exported_at')} {len(pos)}只持仓", flush=True)

    # Mock 客户端种子(paper 模式)
    from execution.qmt_client import MockQMTClient
    client = MockQMTClient()
    client._positions = dict(pos)
    t = Trader()
    t.client = client

    cb_p = ("110", "111", "113", "118", "123", "127", "128")
    stocks = {k: v for k, v in pos.items() if k.split(".")[0][:3] not in cb_p}
    stock_codes = sorted({k.split(".")[0] for k in stocks})
    print(f"股票域: {len(stocks)}只, 总市值 "
          f"{sum(v.get('market_value',0) for v in stocks.values()):,.0f}",
          flush=True)

    # ① 单票市值>10万 检查(需要分笔)
    big = [(c, v.get("market_value", 0)) for k, v in stocks.items()
           for c in [k.split(".")[0]] if v.get("market_value", 0) > 100_000]
    print(f"① 单票>10万(需分笔): {big if big else '无 — 全部单笔可出'}", flush=True)

    # ④ 跌停距离检查(用实时市值/股数 vs 板块跌停价)
    st_map = _build_st_map()
    near_limit = []
    for k, v in stocks.items():
        c = k.split(".")[0]
        if v.get("volume") and v.get("market_value"):
            px = v["market_value"] / v["volume"]
            lim = _limit_prices([c], {c: px}, st_map).get(c, {})
            if lim:
                dist = px / lim["down"] - 1
                if dist < 0.03:
                    near_limit.append((c, f"距跌停{dist*100:.1f}%"))
    print(f"④ 距跌停<3%(周一可能禁卖排队): {near_limit if near_limit else '无'}",
          flush=True)

    # bear 信号构造 + 完整执行
    sig = {"signal_date": "2026-09-07", "regime": "bear",
           "position_ratio": 0.30, "holdings": [],
           "sell": stock_codes, "shares": {}, "prices": {},
           "capital": 1_000_000}
    sig_path = Path("logs/rehearse_bear_signal.json")
    sig_path.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
    res = t.execute_signal(str(sig_path), "track_a")

    sells = res.get("sells", [])
    blocked = res.get("blocked", [])
    print(f"\n排练结果: 卖出 {len(sells)} 笔 / 拦截 {len(blocked)} 笔", flush=True)
    # ③ tick 合法性
    bad_tick = [s for s in sells
                if s.get("price") and round(s["price"], 2) != round(s["price"], 3)]
    print(f"③ tick合法性: 全部通过" if not bad_tick
          else f"③ ⚠️ 非tick价格: {bad_tick}", flush=True)
    # 卖单覆盖度
    sold_codes = {s["code"] for s in sells}
    missing = set(stock_codes) - sold_codes
    print(f"卖单覆盖: {len(sold_codes)}/{len(stock_codes)}只"
          f"{' ⚠️ 未覆盖: ' + str(sorted(missing)) if missing else ' ✓ 全部覆盖'}",
          flush=True)
    if blocked:
        print("风控拦截:", [(b.get("code"), b.get("reason")) for b in blocked],
              flush=True)
    # ② 在途净额: Mock无在途订单, 验证不重复(持仓全部清一次)
    remain = {k: v for k, v in client._positions.items()
              if k.split(".")[0][:3] not in cb_p}
    print(f"② 执行后Mock股票持仓剩余: {len(remain)}只"
          f"{' ✓ 全部清空' if not remain else ' ⚠️ 残留: ' + str(list(remain)[:5])}",
          flush=True)


if __name__ == "__main__":
    main()
