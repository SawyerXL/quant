import sys, os, subprocess

print("Python executable:", sys.executable)
print("Python version:", sys.version)

# Try pip
result = subprocess.run(
    [sys.executable, '-m', 'pip', 'install', 'xtquant', '--target', sys.path[0]],
    capture_output=True, text=True, timeout=60
)
print("pip stdout:", result.stdout[:500])
print("pip stderr:", result.stderr[:500])
print("returncode:", result.returncode)

# Check if xtquant now exists in mpython
mpython = sys.path[0]
if os.path.exists(os.path.join(mpython, 'xtquant')):
    print("xtquant installed to mpython!")
    import xtquant
    print("xtquant import OK:", dir(xtquant))
else:
    print("xtquant not in mpython yet")
    print("Files in mpython:", os.listdir(mpython))
