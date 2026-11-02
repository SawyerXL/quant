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


def read_basket(path):
    """Read code,volume pairs with standard open().

    QMT's ContextInfo.load_stk_vol_list renames/consumes the source file
    (observed 2026-11-02: target_a_g0.csv -> target_a_g0<timestamp>_4.csv),
    so it cannot be called every timer. Standard open() works in simulation
    (heartbeat_ok); use it here and test live separately.
    """
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        raw = f.read().strip()
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    out = {}
    for i in range(0, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1]
    return out


def p1_timer(ContextInfo):
    print("[P1] timer", ContextInfo.barpos)
    for name in BASKETS:
        path = os.path.join(SIGNAL_DIR, name)
        try:
            data = read_basket(path)
            if data is None:
                print("[P1] missing", path)
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
