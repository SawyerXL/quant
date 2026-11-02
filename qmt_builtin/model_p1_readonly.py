#coding:gbk
# QMT builtin Python P1 read-only spike. No passorder / no orders.
# Copy into QMT model editor (Model Research -> Python model).
# This file is intentionally standalone: it does not import repo modules
# and does not modify the existing external execution chain.

import os

SIGNAL_DIR = "C:/quant_live/signals"
ACCOUNT_ID = "REPLACE_WITH_ACCOUNT_ID"
BASKETS = ("target_a_g0.csv", "target_a_g1.csv", "target_cb.csv")


def init(ContextInfo):
    print("[P1] init")
    # Historical start => timer starts immediately (doc: run_time).
    ContextInfo.run_time("p1_timer", "30nSecond", "2026-01-01 09:30:00", "SH")


def p1_timer(ContextInfo):
    print("[P1] timer", ContextInfo.barpos)
    for name in BASKETS:
        path = os.path.join(SIGNAL_DIR, name)
        if not os.path.exists(path):
            print("[P1] missing", path)
            continue
        try:
            data = ContextInfo.load_stk_vol_list(SIGNAL_DIR, name)
            print("[P1] basket", name, data)
        except Exception as e:
            print("[P1] basket_error", name, str(e))
    try:
        pos = get_trade_detail_data(ACCOUNT_ID, "stock", "position")
        print("[P1] positions", len(pos) if pos else 0)
    except Exception as e:
        print("[P1] position_error", str(e))


def handlebar(ContextInfo):
    # Required by QMT; intentionally no-op in P1.
    pass
