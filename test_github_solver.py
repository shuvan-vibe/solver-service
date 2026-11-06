"""Test script to verify if the GitHub Actions Solver Service is alive and providing valid Turnstile tokens.

Usage:
    # Auto-discover URL from MongoDB:
    python test_github_solver.py --mongo "<MONGO_URI>" --key "<SOLVER_API_KEY>"

    # Or specify tunnel URL directly from GitHub Actions log:
    python test_github_solver.py --url "https://xxxx.trycloudflare.com" --key "<SOLVER_API_KEY>"
"""

import sys
import time
import json
import argparse
import urllib.request
import urllib.error

def fetch_json(url, headers=None, timeout=240):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode('utf-8', errors='ignore')
            return resp.status, json.loads(data)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw_error": body}
    except Exception as e:
        return 0, {"error": str(e)}

def discover_url_from_mongo(mongo_uri):
    try:
        from pymongo import MongoClient
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client['socpublic_db']
        status = db['solver_status'].find_one({'_id': 'active'})
        if not status:
            print("❌ No 'active' solver status document found in MongoDB 'solver_status' collection.")
            return None
        print(f"📦 MongoDB Record Found:")
        print(f"   • URL: {status.get('url')}")
        print(f"   • Healthy: {status.get('healthy')}")
        print(f"   • Network Mode: {status.get('network_mode')}")
        print(f"   • Updated At: {status.get('updated_at')}")
        return status.get('url')
    except Exception as e:
        print(f"❌ Failed to query MongoDB: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Test GitHub Actions Solver Service")
    parser.add_argument("--url", help="Public Cloudflare tunnel URL (e.g. https://xxx.trycloudflare.com)")
    parser.add_argument("--key", default="", help="SOLVER_API_KEY")
    parser.add_argument("--mongo", help="MongoDB connection URI to auto-discover URL")
    args = parser.parse_args()

    url = args.url
    if not url and args.mongo:
        print("🔍 Searching MongoDB for active solver URL...")
        url = discover_url_from_mongo(args.mongo)

    if not url:
        print("❌ Error: No solver URL provided.")
        print("Provide either --url (from GitHub Actions run log) or --mongo.")
        print("\nExample:")
        print('  python test_github_solver.py --url https://example.trycloudflare.com --key my-secret-key')
        sys.exit(1)

    url = url.rstrip("/")
    key = args.key.strip()
    headers = {"X-API-Key": key} if key else {}

    print(f"\n==================================================")
    print(f"🚀 Testing Solver Service at: {url}")
    print(f"==================================================")

    # 1. Test Root Liveness
    print("\n[1/3] Testing Root Endpoint (GET /)...")
    code, resp = fetch_json(f"{url}/", timeout=15)
    print(f"      Status: HTTP {code}")
    print(f"      Response: {json.dumps(resp)}")
    if code != 200:
        print("❌ Root endpoint failed! Tunnel might be offline or URL is invalid.")
        sys.exit(1)
    print("      ✅ Root endpoint OK!")

    # 2. Test /health
    print("\n[2/3] Testing Health Endpoint (GET /health)...")
    code, resp = fetch_json(f"{url}/health", headers=headers, timeout=20)
    print(f"      Status: HTTP {code}")
    print(f"      Response: {json.dumps(resp, indent=2)}")
    if code == 401:
        print("❌ HTTP 401 Unauthorized: Invalid SOLVER_API_KEY!")
        sys.exit(1)
    elif code != 200:
        print(f"❌ Health endpoint returned HTTP {code}.")
        sys.exit(1)
    print("      ✅ Health check passed!")

    # 3. Test /getSocpublicToken
    print("\n[3/3] Requesting Real Turnstile Token (GET /getSocpublicToken)...")
    print("      ⏳ Solving Turnstile via SeleniumBase UC + Xray VPN (may take 15-45s)...")
    start = time.time()
    code, resp = fetch_json(f"{url}/getSocpublicToken", headers=headers, timeout=240)
    elapsed = round(time.time() - start, 2)
    print(f"      Status: HTTP {code} (Elapsed: {elapsed}s)")

    if code == 200 and resp.get("success") and resp.get("token"):
        token = resp["token"]
        print(f"\n🎉 SUCCESS! Valid Turnstile Token Acquired!")
        print(f"   • Token prefix: {token[:40]}...")
        print(f"   • Token length: {len(token)} characters")
        print(f"   • Solver duration: {resp.get('elapsed_s', elapsed)}s")
        print(f"   • Attempts: {resp.get('attempts', 1)}")
        print("\n✅ GitHub Action Solver Service is functioning properly and ready to be integrated into Main-Scanner!")
    else:
        print(f"\n❌ Solve failed!")
        print(f"   Response: {json.dumps(resp, indent=2)}")
        sys.exit(1)

if __name__ == "__main__":
    main()

