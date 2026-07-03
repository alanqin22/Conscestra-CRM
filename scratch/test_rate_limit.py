"""Tests for security hardening #4 — auth rate limiting + CORS allowlist.

Part 1 (unit): SlidingWindowLimiter behaviour — no server/DB needed.
Part 2 (live): requires the server running on localhost:8000 —
  • /auth/signin returns 401 for bad creds, then 429 after the per-account
    fail limit; a per-IP flood also 429s.
  • CORS preflight: allowed origin gets Access-Control-Allow-Origin; a
    disallowed origin gets none.

Run:  python scratch/test_rate_limit.py [--live]
"""
import sys
import time

sys.path.insert(0, ".")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def unit_tests():
    print("== unit: SlidingWindowLimiter ==")
    from app.core.rate_limit import SlidingWindowLimiter

    lim = SlidingWindowLimiter(max_events=3, window_seconds=0.5)
    check("under limit not limited", not lim.is_limited("k"))
    for _ in range(3):
        lim.record("k")
    check("at limit is limited", lim.is_limited("k"))
    check("other key unaffected", not lim.is_limited("other"))
    lim.reset("k")
    check("reset clears the window", not lim.is_limited("k"))
    for _ in range(3):
        lim.record("k")
    time.sleep(0.6)
    check("window expiry unlocks", not lim.is_limited("k"))
    check("record returns in-window count", lim.record("c") == 1 and lim.record("c") == 2)


def live_tests(base="http://localhost:8000"):
    import requests

    from app.core.rate_limit import MAX_FAILS_PER_USER as LIMIT

    print(f"== live: /auth/signin throttle (per-account fail limit {LIMIT}) ==")
    ident = f"rate-limit-test-{int(time.time())}@nowhere.invalid"
    codes = []
    for _ in range(LIMIT + 2):
        r = requests.post(f"{base}/auth/signin",
                          json={"identifier": ident, "password": "wrong-password"},
                          timeout=30)
        codes.append(r.status_code)
    print(f"  status sequence: {codes}")
    check(f"first {LIMIT} attempts are 401", all(c == 401 for c in codes[:LIMIT]))
    check(f"locked out with 429 after {LIMIT} fails",
          codes[LIMIT] == 429 and codes[LIMIT + 1] == 429)

    print("== live: CORS allowlist ==")
    headers = {"Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "content-type"}
    ok = requests.options(f"{base}/auth/signin", timeout=10,
                          headers={**headers, "Origin": "https://agentorc.ca"})
    bad = requests.options(f"{base}/auth/signin", timeout=10,
                           headers={**headers, "Origin": "https://evil.example.com"})
    check("agentorc.ca preflight allowed",
          ok.headers.get("access-control-allow-origin") == "https://agentorc.ca",
          f"got {ok.status_code} {dict(ok.headers)}")
    check("evil origin preflight refused",
          "access-control-allow-origin" not in {k.lower() for k in bad.headers},
          f"got {bad.status_code} {dict(bad.headers)}")


if __name__ == "__main__":
    unit_tests()
    if "--live" in sys.argv:
        live_tests()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
