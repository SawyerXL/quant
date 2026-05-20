import sys, os

print("=== callback test ===")
print("Python:", sys.version)

# Test 1: check if init/handleBar are expected by the terminal
# These are standard QMT callback signatures
print("globals before:", [k for k in globals() if not k.startswith('_')])

def init(ContextInfo):
    print("INIT CALLED! type:", type(ContextInfo))
    if ContextInfo is not None:
        attrs = [a for a in dir(ContextInfo) if not a.startswith('_')]
        print("ContextInfo attrs:", attrs[:30])
    else:
        print("ContextInfo is None")

def handleBar(ContextInfo):
    print("HANDLEBAR CALLED! type:", type(ContextInfo))
    if ContextInfo is not None:
        attrs = [a for a in dir(ContextInfo) if not a.startswith('_')]
        print("ContextInfo attrs:", attrs[:30])

print("functions defined, waiting for callbacks...")
print("If you see only this line and no INIT CALLED, callbacks are not used")
