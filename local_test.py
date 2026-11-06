"""Run a real Socpublic solve locally.

Usage:
    python local_test.py
    python local_test.py --sitekey 0x... --pageurl https://...
"""

import argparse
import asyncio
import logging

import config
import xray_manager
from main import solve_turnstile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sitekey", default=config.SOCPUBLIC_SITEKEY)
    parser.add_argument("--pageurl", default=config.SOCPUBLIC_PAGEURL)
    parser.add_argument("--no-xray", action="store_true")
    args = parser.parse_args()

    if config.NETWORK_MODE == "xray" and not args.no_xray:
        ok = await xray_manager.ensure_running()
        if not ok:
            print("Xray tunnel failed to start")
            return 1

    token = await asyncio.to_thread(solve_turnstile, args.sitekey, args.pageurl)
    if token:
        print("\n[SUCCESS] Token obtained")
        print(f"token length : {len(token)}")
        print(f"token        : {token[:80]}...")
        return 0

    print("\n[FAILED] No token")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
