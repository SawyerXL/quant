"""①b 参数一致性核对补齐: 10-08 被拒 8 笔 vs 10-09 成交, 三只(110081/113056/113062)逐字段比对。

口径(35db70c 已锁): 身份字段=代码/方向/张数/账户/人工单; 限价/状态/成交量为
预期差异字段(跨日价格刷新/拒单本身)。考卷=trade_plan_20261008_cb.json 只删不改。
"""
import json

REJ = "/root/quant/logs/reject_reasons_20261008.json"
FILL = "/root/quant/logs/qmt_orders_20261009.json"
PLAN = "/root/quant/logs/trade_plan_20261008_cb.json"

rej = json.load(open(REJ))
fill_all = {o["code"]: o for o in json.load(open(FILL))["orders"]}
plan_sells = {s["code"]: s for s in json.load(open(PLAN))["sells"]}

print("=== 10-08 被拒单 (8 笔) ===")
for r in rej:
    print(f'{r["code"]} {r["direction"]} {r["volume"]}张 status={r["status"]} filled={r["filled"]} msg={r["msg"]}')

print()
print("=== 逐字段比对 (身份字段: 代码/方向/张数/账户/人工单) ===")
print(f'{"字段":<10}{"110081":<28}{"113056":<28}{"113062":<28}')
rows = [
    ("代码", lambda c: (c, c)),
    ("方向", lambda c: (rej_side(c), fill_all[c]["direction"])),
    ("张数", lambda c: (rej_vol(c), fill_all[c]["shares"])),
    ("考卷张数", lambda c: (plan_sells[c]["shares"], fill_all[c]["shares"])),
    ("人工单", lambda c: (rej_rmk(c), fill_all[c]["remark"])),
    ("状态", lambda c: (57, fill_all[c]["status"])),
    ("成交量", lambda c: (0, fill_all[c]["filled"])),
    ("限价", lambda c: (rej_price(c), fill_all[c]["limit_price"])),
]

def rej_entries(code):
    return [r for r in rej if r["code"].startswith(code)]

def rej_side(code):
    return set(r["direction"] for r in rej_entries(code)).pop()

def rej_vol(code):
    return set(r["volume"] for r in rej_entries(code)).pop()

def rej_od(code):
    od = json.load(open("/root/quant/logs/qmt_orders_20261008.json"))["orders"]
    return [o for o in od if o["code"].startswith(code)]

def rej_rmk(code):
    # reject_reasons 文件无 remark 字段; 从 qmt_orders_20261008.json 取
    return set(o["remark"] for o in rej_od(code)).pop()

def rej_price(code):
    return sorted(set(o["limit_price"] for o in rej_od(code)))

for label, fn in rows:
    out = []
    for code in ["110081", "113056", "113062"]:
        if label == "限价":
            a = fn(code)[0]
            b = fn(code)[1]
            out.append(f"{a}→{b}")
        elif label == "人工单":
            a, b = fn(code)
            mark = "✅" if a == b else f"❌ {a!r}≠{b!r}"
            out.append(f"{a!r} vs {b!r} {mark}")
        else:
            a, b = fn(code)
            if label in ("状态", "成交量", "限价"):
                out.append(f"{a}→{b} (预期差异)" if a != b else f"{a} (意外一致)")
            else:
                mark = "✅" if a == b else f"❌ {a}≠{b}"
                out.append(f"{a} vs {b} {mark}")
    print(f"{label:<10}{out[0]:<28}{out[1]:<28}{out[2]:<28}")

print()
# 人工单标记约定反证: 10-09 链单 127027 带 sell_N remark
chain_rmk = {o["code"]: o["remark"] for o in json.load(open(FILL))["orders"] if o["remark"]}
print("=== 链单标记反证 (remark 非空=链自动) ===")
print(chain_rmk)
manual_ok = all(fill_all[c]["remark"] == "" for c in ["110081", "113056", "113062"])
print("三只 10-09 成交单 remark 均为空 → 人工单:", manual_ok)
