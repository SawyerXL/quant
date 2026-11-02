#coding:gbk
# QMT builtin Python P1 read-only spike. No passorder / no orders.
# Copy into QMT model editor (Model Research -> Python model).
# This file is intentionally standalone: it does not import repo modules
# and does not modify the existing external execution chain.

import os

SIGNAL_DIR = "C:/quant_live/p1_baskets"
ACCOUNT_ID = "REPLACE_WITH_ACCOUNT_ID"
BASKETS = ("target_a_g0.csv", "target_a_g1.csv", "target_cb.csv")
HEARTBEAT = "C:/quant_live/p1_baskets/p1_heartbeat.txt"


def init(ContextInfo):
    print("[P1] init")
    # Historical start => timer starts immediately (doc: run_time).
    ContextInfo.run_time("p1_timer", "30nSecond", "2026-01-01 09:30:00", "SH")


BASKET_CACHE = {}


def load_basket_once(ContextInfo, name):
    """QMT's documented basket loader is one-shot: it renames/consumes the
    source file after reading. Cache the dict so later timers do not miss the
    renamed file. Create exact source files again before each fresh P1 run.
    """
    if name in BASKET_CACHE:
        return BASKET_CACHE[name]
    data = ContextInfo.load_stk_vol_list(SIGNAL_DIR, name)
    BASKET_CACHE[name] = data
    return data


def p1_timer(ContextInfo):
    print("[P1] timer", ContextInfo.barpos)
    for name in BASKETS:
        try:
            data = load_basket_once(ContextInfo, name)
            if not data:
                print("[P1] missing_or_empty", name)
            else:
                print("[P1] basket", name, data)
        except Exception as e:
            print("[P1] basket_error", name, str(e))
    npos = -1
    try:
        pos = get_trade_detail_data(ACCOUNT_ID, "stock", "position")
        npos = len(pos) if pos else 0
        print("[P1] positions", npos)
    except Exception as e:
        print("[P1] position_error", str(e))
    # P1 file-I/O test: standard open is not documented; try/except makes
    # the result explicit instead of assuming it works.
    try:
        f = open(HEARTBEAT, "a")
        f.write("barpos=%s positions=%s\n" % (ContextInfo.barpos, npos))
        f.close()
        print("[P1] heartbeat_ok")
    except Exception as e:
        print("[P1] file_error", str(e))


def handlebar(ContextInfo):
    # Required by QMT; intentionally no-op in P1.
    pass
