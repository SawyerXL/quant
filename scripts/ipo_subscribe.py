"""
新股申购脚本（2026-09-02，队列#3 打新 +0.5~1.5pp/年，Windows QMT 端运行）。

流程: query_ipo_data 拿今日可申购清单 → 规则过滤(板块/跳过名单/额度)
     → 顶格申购 → 企业微信告警。
安全: DRY_RUN 默认开(只打印不下单); 首次实盘建议先观察 QMT 委托面板
     确认下单模式正确(FIX_PRICE+0价=申购委托)再关 dry-run。

规则(IPO_RULES, 可改):
  - boards: 允许自动申购的板块(60主板/00主板/30创业/68科创; 北交所
    8/4/92 需要现金缴款, 默认关)
  - skip_codes: 跳过名单(破发倾向个股人工加)
  - min_price: 发行价低于此价才申购(低价发行=常见炒作标的, 高价=破发
    风险面大; 阈值用户定, 默认 0=不启用价格过滤)
用法: python scripts/ipo_subscribe.py            # dry-run 预览
      python scripts/ipo_subscribe.py --go        # 实盘申购
计划任务建议: 每个交易日 09:20(申购窗口 9:30-15:00, 越早越好)。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from monitoring.alerts import send_alert

# ═══════════════ 申购规则(用户可改) ═══════════════
IPO_RULES = {
    "boards": {"60", "00", "30", "68"},   # 允许的板块前缀(北交所默认关: 现金缴款制)
    "skip_codes": set(),                   # 人工跳过名单
    "min_price": 0.0,                      # 发行价下限(0=不启用)
    "max_price": 0.0,                      # 发行价上限(0=不启用; 高价发行破发面大)
}
STATE_FILE = Path("logs/ipo_subscribed.json")


def _today_subscribed() -> set:
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if d.get("date") == date.today().strftime("%Y-%m-%d"):
                return set(d.get("codes", []))
        except Exception:
            pass
    return set()


def _save_subscribed(codes: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"date": date.today().strftime("%Y-%m-%d"), "codes": sorted(codes)},
        indent=2), encoding="utf-8")


def _fetch_ipo_list():
    """query_ipo_data: 今日可申购新股 [{code, name, price, 可申购数量}...]"""
    from execution.qmt_client import get_client
    client = get_client()
    try:
        raw = client.trader.query_ipo_data()
    except (AttributeError, TypeError):
        raw = None
    if not raw:
        return []
    out = []
    for item in raw:
        # 字段名以实盘打印为准, 常见: stock_code/stock_name/issue_price/
        # enable_amount/purchase_limit
        code = getattr(item, "stock_code", "") or getattr(item, "security_code", "")
        name = getattr(item, "stock_name", "") or getattr(item, "security_name", "")
        price = float(getattr(item, "issue_price", 0) or getattr(item, "price", 0) or 0)
        limit = int(getattr(item, "enable_amount", 0) or
                    getattr(item, "purchase_limit", 0) or 0)
        if code:
            out.append({"code": str(code).split(".")[0], "name": name,
                        "price": price, "limit": limit})
    return out


def _apply_rules(ipos):
    picked, skipped = [], []
    for ipo in ipos:
        code = ipo["code"]
        prefix = code[:2]
        reasons = []
        if prefix not in IPO_RULES["boards"]:
            reasons.append(f"板块{prefix}不在允许名单")
        if code in IPO_RULES["skip_codes"]:
            reasons.append("人工跳过名单")
        if IPO_RULES["min_price"] > 0 and ipo["price"] < IPO_RULES["min_price"]:
            reasons.append(f"发行价{ipo['price']}低于下限")
        if IPO_RULES["max_price"] > 0 and ipo["price"] > IPO_RULES["max_price"]:
            reasons.append(f"发行价{ipo['price']}高于上限")
        if ipo["limit"] <= 0:
            reasons.append("额度为0")
        (skipped if reasons else picked).append(
            (ipo, "; ".join(reasons)))
    return picked, skipped


def main(go: bool):
    from execution.qmt_client import get_client
    client = get_client()
    ipos = _fetch_ipo_list()
    if not ipos:
        logger.info("今日无可申购新股(或query_ipo_data为空)")
        return
    picked, skipped = _apply_rules(ipos)
    done = _today_subscribed()

    msg_lines = [f"[打新] {date.today()} 候选{len(ipos)}只 申购{len(picked)}只 跳过{len(skipped)}只"]
    for ipo, reason in skipped:
        msg_lines.append(f"  跳过 {ipo['name']}({ipo['code']}) 价{ipo['price']}: {reason}")
    for ipo, _ in picked:
        if ipo["code"] in done:
            msg_lines.append(f"  已申购(去重) {ipo['name']}")
            continue
        msg_lines.append(f"  {'[DRY]' if not go else '申购'} {ipo['name']}({ipo['code']}) "
                         f"价{ipo['price']} 顶格{ipo['limit']}股")
        if go:
            # FIX_PRICE+0价 = 新股申购委托(QMT识别); 失败仅告警不中断
            try:
                oid = client.place_order(ipo["code"], "buy", ipo["limit"],
                                         0.0, "limit")
                if oid >= 0:
                    done.add(ipo["code"])
                else:
                    msg_lines.append(f"    ⚠️ 下单失败 code={ipo['code']}")
            except Exception as e:
                msg_lines.append(f"    ⚠️ 下单异常 {e}")
    if go:
        _save_subscribed(done)
    send_alert("\n".join(msg_lines))
    logger.info("\n".join(msg_lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="实盘申购(默认dry-run)")
    main(ap.parse_args().go)
