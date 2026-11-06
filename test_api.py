"""Smoke-test the running solver API.

Usage:
    python test_api.py --url http://127.0.0.1:8080 --key local-dev-key-12345
    python test_api.py --url http://127.0.0.1:8080 --key ... --solve
"""

import argparse
import json
import urllib.error
import urllib.request


def call(url, key, path, timeout=240):
    req = urllib.request.Request(url.rstrip("/") + path)
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--key", default="")
    parser.add_argument("--solve", action="store_true")
    args = parser.parse_args()

    status, body = call(args.url, args.key, "/health", timeout=30)
    print(f"/health -> {status}")
    try:
        print(json.dumps(json.loads(body), indent=2))
    except Exception:
        print(body)

    if args.solve:
        status, body = call(args.url, args.key, "/getSocpublicToken")
        print(f"/getSocpublicToken -> {status}")
        try:
            data = json.loads(body)
            if data.get("token"):
                data["token"] = data["token"][:60] + "..."
            print(json.dumps(data, indent=2))
        except Exception:
            print(body)


if __name__ == "__main__":
    main()
