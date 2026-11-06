"""Test the on-demand Xray tunnel without touching the browser."""

import asyncio
import logging

import aiohttp

import config
import xray_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    print(f"network_mode : {config.NETWORK_MODE}")
    print(f"xray_binary  : {config.XRAY_BINARY}")
    print(f"vless        : {config.XRAY_VLESS_CONFIG[:40]}...")

    ok = await xray_manager.ensure_running()
    print(f"xray ready   : {ok}")
    print(f"status       : {xray_manager.status()}")

    if ok:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.ipify.org?format=json",
                    proxy=xray_manager.get_http_proxy_url(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    print(f"exit ip      : {await resp.text()}")
        except Exception as exc:
            print(f"exit ip check failed: {exc}")

    xray_manager.stop_xray()
    print("xray stopped")


if __name__ == "__main__":
    asyncio.run(main())
