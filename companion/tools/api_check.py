"""Developer tool: exercises the companion's session-switching API over HTTP without touching playback.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\api_check.py [--port 8790] [--host 127.0.0.1]

Only selection requests are sent (source-next / source-prev / source-auto) plus a control aimed at a session that does not exist
(which must be refused). Nothing is played, paused, skipped or volume-changed. Run it against a SEPARATE companion instance
(`python -m desk_media_companion --port 8790 --no-discovery`) so the real display's selection is not disturbed.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str) -> dict:
    req = urllib.request.Request(base + path, method=method, headers={"X-Desk-Client": "api-check"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, **json.loads(exc.read().decode("utf-8") or "{}")}


def brief(now: dict) -> str:
    return (f"[{now.get('app')}] {now.get('title')!r} state={now.get('state')} source={now.get('source')!r} "
            f"{now.get('source_index')}/{now.get('sources')} mode={now.get('mode')} can_play={now.get('can_play')} art={now.get('art_id')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"
    failures = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS  " if cond else "FAIL  ") + name + (("  - " + detail) if detail else ""))
        if not cond:
            failures.append(name)

    call(base, "POST", "/control/source-auto")
    srcs = call(base, "GET", "/sources")
    print(json.dumps(srcs, indent=1))
    rows = srcs["sources"]
    n = len(rows)
    check("sources listed", srcs.get("ok") and n >= 1, f"{n} source(s): " + ", ".join(r["name"] for r in rows))
    start = srcs["displayed"]
    now = call(base, "GET", "/now-playing")
    print("start:", brief(now))
    check("snapshot carries the source fields", now.get("sources") == n and now.get("source_index", 0) >= 1 and now.get("source"))
    if n < 2:
        print("only one source: a swipe must do nothing")
        r = call(base, "POST", "/control/source-next?from=" + start)
        check("single source: changed=false", r.get("ok") and r.get("changed") is False, json.dumps(r)[:160])
        return 1 if failures else 0

    names = [r["app"] for r in rows]
    # walk the whole ring forwards, then backwards, starting from what is displayed
    here = start
    seen = [here]
    for _ in range(n):
        r = call(base, "POST", "/control/source-next?from=" + here)
        check(f"next from {here}", r.get("ok") and r.get("changed") and r["now"]["app"] == r["app"], f"-> {r.get('app')} ({r.get('index')}/{r.get('count')})")
        print("   now:", brief(r["now"]))
        here = r["app"]
        seen.append(here)
    check("a full forward lap returns to the start (wrap-around)", seen[-1] == start and sorted(set(seen)) == sorted(names), " -> ".join(seen))
    now = call(base, "GET", "/now-playing")
    check("the display stays on the selected session (nothing else started playing)", now["app"] == start and now["mode"] in ("manual", "auto"), brief(now))

    seen = [here]
    for _ in range(n):
        r = call(base, "POST", "/control/source-prev?from=" + here)
        check(f"previous from {here}", r.get("ok") and r.get("changed"), f"-> {r.get('app')}")
        here = r["app"]
        seen.append(here)
    check("a full backward lap returns to the start", seen[-1] == start, " -> ".join(seen))

    # a stale `from` (the display shows a session that is gone) is handled relative to what the companion shows
    r = call(base, "POST", "/control/source-next?from=no-such-app.exe")
    check("stale 'from' does not break switching", r.get("ok") and r.get("changed"), f"-> {r.get('app')}")

    # commands aimed at a session that does not exist must be refused, not redirected to another app
    for action in ("playpause", "next", "volume-up"):
        r = call(base, "POST", f"/control/{action}?app=no-such-app.exe")
        check(f"{action} aimed at a missing session is refused", r.get("ok") is False, json.dumps(r)[:120])

    r = call(base, "POST", "/control/source-auto")
    check("back to automatic", r.get("ok") and r["now"]["mode"] == "auto", brief(r["now"]))
    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
