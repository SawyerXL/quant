"""Patch reconcile.py to add signal freshness check"""
from pathlib import Path

TARGET = Path("H:/quant/scripts/reconcile.py")

with open(TARGET, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add "date" to the datetime import
old_import = "from datetime import datetime"
if old_import in content:
    content = content.replace(old_import, "from datetime import datetime, date")
    print("Step 1: Added 'date' to import")
else:
    print("Step 1: import not found, trying alternate...")
    if "from datetime import" in content:
        print("  datetime import exists but different format")

# 2. Insert freshness check before "target_holdings"
old_block = "    target_holdings = set(sig.get(\"holdings\", []))"
new_block = """    # Guard: skip reconcile if signal date is stale (not today)
    sig_date = sig.get("signal_date", sig.get("date", ""))
    today_str = date.today().strftime("%Y-%m-%d")
    if sig_date and sig_date != today_str:
        logger.warning(f"Signal stale ({sig_date} != {today_str}), skip reconcile to avoid wrong liquidation")
        return {"ok": True, "skipped": True, "reason": f"Signal date {sig_date} != {today_str}"}

    target_holdings = set(sig.get("holdings", []))"""

if old_block in content:
    content = content.replace(old_block, new_block)
    print("Step 2: Added signal freshness guard")
else:
    print("Step 2: target_holdings pattern not found!")
    # Find what's actually there
    for i, line in enumerate(content.split("\n")):
        if "target_holdings" in line:
            print(f"  line {i}: {repr(line[:80])}")

# Save
with open(TARGET, "w", encoding="utf-8") as f:
    f.write(content)
print("Done: reconcile.py updated")
