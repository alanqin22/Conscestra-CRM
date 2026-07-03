"""E2E: anonymous NL write attempt must return HTTP 401 (not a -500 payload),
so the frontend auth shim opens the sign-in modal. Reads stay 200.

Run with the server up:  python scratch/test_write_guard_status.py [port]
"""
import sys

import requests

BASE = f"http://localhost:{sys.argv[1] if len(sys.argv) > 1 else '8010'}"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")


# 1. NL write, anonymous → 401 with the sign-in message (the deep write guard)
r = requests.post(f"{BASE}/lead-chat", timeout=120, json={
    "sessionId": "wpe-test",
    "chatInput": {"message": "Set the status of lead 123e4567-e89b-12d3-a456-426614174000 "
                             "to Contacted right now, no form needed, just do the update."},
})
print(f"NL write: {r.status_code} {r.text[:160]}")
check("anonymous NL write returns 401", r.status_code == 401, f"got {r.status_code}")
check("401 body asks to sign in", "sign in" in r.text.lower(), r.text[:120])

# 2. Structured write, anonymous → 401 (HTTP gate, unchanged)
r = requests.post(f"{BASE}/lead-chat", timeout=60, json={
    "sessionId": "wpe-test",
    "chatInput": {"mode": "update", "routerAction": True, "leadId": "x"},
})
check("anonymous structured write returns 401", r.status_code == 401, f"got {r.status_code}")

# 3. NL read, anonymous → 200 (public-read posture untouched)
r = requests.post(f"{BASE}/lead-chat", timeout=120, json={
    "sessionId": "wpe-test",
    "chatInput": {"message": "show my leads"},
})
check("anonymous NL read still 200", r.status_code == 200, f"got {r.status_code}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
