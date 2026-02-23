#!/usr/bin/env python3
# stdlib-only self-check crawler (no JS needed)
import re, sys, urllib.parse, urllib.request, http.cookiejar

HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
HTML_TAG = re.compile(br'<\s*html\b', re.I)

def norm(base, href):
    if not href: return None
    href = href.strip()
    if href.startswith(("#","mailto:","javascript:")): return None
    u = urllib.parse.urljoin(base, href)
    pu = urllib.parse.urlparse(u)
    pb = urllib.parse.urlparse(base)
    if pu.scheme != pb.scheme or pu.netloc != pb.netloc: return None
    path = pu.path + (("?" + pu.query) if pu.query else "")
    return path or "/"

def get(opener, base, path):
    url = urllib.parse.urljoin(base, path)
    r = opener.open(url, timeout=10)
    data = r.read()
    return r.getcode(), dict(r.headers), data

def post_form(opener, base, path, fields):
    url = urllib.parse.urljoin(base, path)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    r = opener.open(urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type":"application/x-www-form-urlencoded"}), timeout=10)
    data = r.read()
    return r.getcode(), dict(r.headers), data

def crawl(base, login_path, user, pw, starts, limit=250):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    fails, ok = [], []
    if login_path:
        try:
            st, hd, _ = post_form(opener, base, login_path, {"username":user, "password":pw, "next":"/"})
            ok.append(f"LOGIN {st} {login_path} as {user}")
        except Exception as e:
            fails.append(f"LOGIN ERROR {login_path} as {user} :: {e}")
            return ok, fails

    q = [p if p.startswith("/") else "/" + p for p in starts]
    seen = set()
    while q and len(seen) < limit:
        p = q.pop(0)
        if p in seen: continue
        seen.add(p)
        try:
            st, hd, data = get(opener, base, p)
            bad = (st >= 400) or (b"{{" in data) or (not HTML_TAG.search(data) and (hd.get("Content-Type","").startswith("text/html")))
            if bad:
                snippet = data[:120].decode("utf-8","ignore").replace("\n"," ")
                fails.append(f"FAIL {st} {p} :: {snippet}")
            else:
                ok.append(f"OK   {st} {p}")
            # enqueue links
            for m in HREF.finditer(data.decode("utf-8","ignore")):
                np = norm(base, m.group(1))
                if np and np not in seen:
                    q.append(np)
        except Exception as e:
            fails.append(f"ERROR 0 {p} :: {e}")
    ok.append(f"VISITED {len(seen)} pages")
    return ok, fails

if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    login_path = sys.argv[2] if len(sys.argv) > 2 else ""
    user = sys.argv[3] if len(sys.argv) > 3 else ""
    pw = sys.argv[4] if len(sys.argv) > 4 else ""
    starts = sys.argv[5:] or ["/"]
    ok, fails = crawl(base, login_path, user, pw, starts)
    print("=== SELF-CHECK REPORT ===")
    print("BASE:", base)
    if login_path:
        print("LOGIN:", login_path, "USER:", user)
    print("STARTS:", starts)
    print("\n-- FAILURES --")
    print("\n".join(fails) if fails else "None")
    print("\n-- OK (first 40) --")
    print("\n".join(ok[:40]))
