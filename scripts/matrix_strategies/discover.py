import os
import sys

base = os.path.dirname(sys.path[0])
bin64 = os.path.join(base, "bin.x64")

print("=== sys.path ===")
for p in sys.path:
    print(p)

print("\n=== bin.x64 pyd files ===")
if os.path.exists(bin64):
    files = os.listdir(bin64)
    for f in files:
        if f.endswith('.pyd') or f.endswith('.dll') and any(k in f.lower() for k in ['xt','trade','order','quant']):
            print(f)

print("\n=== lib directory ===")
lib = os.path.join(bin64, "lib")
if os.path.exists(lib):
    for f in os.listdir(lib)[:20]:
        print(f)

print("\n=== site-packages ===")
site = os.path.join(lib, "site-packages")
if os.path.exists(site):
    for f in os.listdir(site):
        print(f)
else:
    print("site-packages not found")

print("\n=== try imports ===")
for mod in ['xtquant','xt','XtQuant','trade','dealer','broker','order','account','market']:
    try:
        m = __import__(mod)
        print(f"OK: {mod} -> {dir(m)[:5]}")
    except ImportError:
        print(f"NO: {mod}")

print("\n=== signal file ===")
sig = r"H:\quant\data_store\meta\signal_a_latest.json"
print("exists:", os.path.exists(sig))
