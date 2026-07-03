"""Anonymous (public-read) write-mode coverage: every module's write modes must
401 for anonymous callers while reads stay 200.
Run with the server up:  python scratch/test_write_modes.py [port]
"""
import sys

import requests

BASE = f"http://localhost:{sys.argv[1] if len(sys.argv) > 1 else '8010'}"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")


def post(path, chat_input):
    return requests.post(f"{BASE}{path}", timeout=60,
                         json={"sessionId": "write-mode-test", "chatInput": chat_input})


WRITES = [
    ("/accounting-chat",    {"mode": "generate_invoice", "routerAction": True}),
    ("/accounting-chat",    {"mode": "record_payment", "routerAction": True}),
    ("/accounting-chat",    {"mode": "void_invoice", "routerAction": True}),
    ("/lead-chat",          {"mode": "score", "routerAction": True}),
    ("/email-chat",         {"mode": "send_email", "routerAction": True}),
    ("/notifications-chat", {"mode": "mark_all_read", "routerAction": True}),
    ("/order-chat",         {"mode": "advance_statuses", "routerAction": True}),
    ("/contact-chat",       {"mode": "send_verification", "routerAction": True}),
]
READS = [
    ("/accounting-chat",    {"mode": "list_invoices", "routerAction": True}),
    ("/accounting-chat",    {"mode": "account_summary", "routerAction": True}),
    ("/notifications-chat", {"mode": "unread_count", "routerAction": True}),
]

for path, ci in WRITES:
    r = post(path, ci)
    check(f"anon write blocked  {path} mode={ci['mode']}", r.status_code == 401,
          f"got {r.status_code}: {r.text[:100]}")
for path, ci in READS:
    r = post(path, ci)
    check(f"anon read allowed   {path} mode={ci['mode']}", r.status_code == 200,
          f"got {r.status_code}: {r.text[:100]}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
