"""
Smoke test for SayF.Agency No‑JS MVP.

Usage:
1) Terminal A:  python server.py
2) Terminal B:  python tools/smoke_test.py

No external libraries.
"""

import urllib.request
import urllib.parse
import http.cookiejar
import sys
import time

BASE = "http://127.0.0.1:8000"

def req(opener, method, path, data=None):
    url = BASE + path
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method)
    try:
        with opener.open(r, timeout=10) as resp:
            body = resp.read(256)
            return resp.getcode(), dict(resp.headers), body
    except Exception as e:
        return None, {"error": str(e)}, b""

def ok(code):
    return code is not None and 200 <= code < 400

def main():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    tests = []

    # public + static
    tests += [("GET", "/")]
    tests += [("GET", "/static/logo.svg")]
    tests += [("GET", "/static/favicon.ico")]

    # admin login + tabs
    tests += [("GET", "/admin/login")]
    # login (admin/admin)
    code, headers, _ = req(opener, "POST", "/admin/login", {"username": "admin", "password": "admin"})
    print(f"[ADMIN LOGIN] {code} {headers.get('Location','')}")
    # follow redirect if provided
    loc = headers.get("Location")
    if loc:
        req(opener, "GET", loc)

    for tab in ["notifications","leads","calendar","clients","pnl","messages","employees"]:
        tests += [("GET", f"/admin?tab={tab}")]

    # client login + tabs
    tests += [("GET", "/client/login")]
    code, headers, _ = req(opener, "POST", "/client/login", {"username": "DemenageursPlus", "password": "admin"})
    print(f"[CLIENT LOGIN] {code} {headers.get('Location','')}")
    loc = headers.get("Location")
    if loc:
        req(opener, "GET", loc)

    for tab in ["notifications","leads","calendar","clients","pnl","messages"]:
        tests += [("GET", f"/client?tenant=T000&tab={tab}")]

    # Try sample modals (won't fail the whole test if IDs don't exist)
    tests += [("GET", "/admin?tab=leads&modal=view_lead&lead_id=L0001")]
    tests += [("GET", "/admin?tab=calendar&modal=view_event&event_id=E0001")]
    tests += [("GET", "/admin?tab=clients&modal=view_tenant&tenant_id=T000")]

    print("\n[SMOKE] Running requests...")
    bad = 0
    for method, path in tests:
        code, headers, body = req(opener, method, path)
        status = "OK" if ok(code) else "FAIL"
        if status == "FAIL":
            bad += 1
        err = headers.get("error","")
        print(f"{status:4} {method:4} {path:55} -> {code} {err}")

    if bad:
        print(f"\n[RESULT] {bad} failing requests. Check server console traceback for the first failure.")
        sys.exit(1)
    print("\n[RESULT] All requests returned 2xx/3xx.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
