import sys, os, subprocess

print("Python:", sys.executable)
print("Version:", sys.version[:10])

mpython = sys.path[0]
print("Target dir:", mpython)

# Python 3.6 compatible subprocess call (no capture_output)
proc = subprocess.Popen(
    [sys.executable, '-m', 'pip', 'install', 'xtquant', '--target', mpython,
     '--no-warn-script-location'],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)
out, err = proc.communicate(timeout=120)
print("stdout:", out.decode('utf-8', errors='ignore')[:500])
print("stderr:", err.decode('utf-8', errors='ignore')[:300])
print("returncode:", proc.returncode)

# Check result
if os.path.exists(os.path.join(mpython, 'xtquant')):
    print("SUCCESS: xtquant installed!")
    import xtquant
    print("xtquant OK:", dir(xtquant)[:5])
else:
    print("FAILED: xtquant not found in mpython")
    print("Contents:", os.listdir(mpython)[:10])
