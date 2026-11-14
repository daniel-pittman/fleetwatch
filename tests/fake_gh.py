"""A stand-in for `gh api`, driven by a JSON fixture: FAKE_GH_FIXTURE names the file, FAKE_GH_LOG gets one
line per call (the endpoint as asked for, query included).

The fixture maps an endpoint path (no query) to the body to print. A key "PATH page=N" answers page N; any
other page past the first is an empty list. A body of {"__status__": 403} fails the call the way gh does,
with an HTTP code on stderr; {"__sleep__": 5} sleeps first; {"__exit__": 4} exits 4 as a logged-out gh does; {"__neterr__": 1} fails the way gh does when the
connection drops; {"__raw__": "text"} prints text as is.
"""
import json
import os
import sys
import time


def main(argv):
    endpoint = next((a for a in argv if a.startswith("repos/") or a.startswith("/repos/")), None)
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(" ".join(argv) + "\n")
    if not argv or argv[0] != "api" or endpoint is None or "GET" not in argv:
        print("fake gh: only read-only `gh api --method GET repos/...` is supported", file=sys.stderr)
        return 64
    with open(os.environ["FAKE_GH_FIXTURE"], encoding="utf-8") as fh:
        routes = json.load(fh)
    path, _, query = endpoint.lstrip("/").partition("?")
    params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    page = int(params.get("page", "1"))
    key = path if page == 1 else "%s page=%d" % (path, page)
    if key not in routes:
        if page > 1:
            print("[]")
            return 0
        # gh echoes the API's message; the text here stands for outside words that must not be stored
        print("gh: CANARY-STDERR no route %s (HTTP 404)" % path, file=sys.stderr)
        return 1
    body = routes[key]
    if isinstance(body, dict) and "__sleep__" in body:
        time.sleep(body["__sleep__"])
    if isinstance(body, dict) and "__exit__" in body:
        print("CANARY-STDERR to get started with GitHub CLI, please run: gh auth login", file=sys.stderr)
        return body["__exit__"]
    if isinstance(body, dict) and "__neterr__" in body:
        print('Get "https://api.github.com/%s": read tcp 10.0.0.2:51629->192.0.2.5:443: read: connection reset '
              'by peer CANARY-STDERR' % path, file=sys.stderr)
        return 1
    if isinstance(body, dict) and "__status__" in body:
        print("gh: CANARY-STDERR refused (HTTP %d)" % body["__status__"], file=sys.stderr)
        return 1
    if isinstance(body, dict) and "__raw__" in body:
        sys.stdout.write(body["__raw__"])
        return 0
    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
