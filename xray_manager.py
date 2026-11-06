"""On-demand Xray/VLESS tunnel manager.

The tunnel is only started when a solve request needs it and is stopped again
after XRAY_IDLE_TIMEOUT seconds without use.  This keeps the expensive part of
the solver (VPN + browser) off when nobody is logging in.
"""

import asyncio
import base64
import io
import json
import logging
import os
import platform
import subprocess
import time
import zipfile
from urllib.parse import unquote, parse_qs

import aiohttp

import config

logger = logging.getLogger(__name__)

_process = None
_proxy_available = False
_last_used = 0.0
_lock = asyncio.Lock()
_cached_vless = ""


# ---------------------------------------------------------------------------
# Status helpers (safe to call from sync code / threads)
# ---------------------------------------------------------------------------
def is_running() -> bool:
    return _process is not None and _process.poll() is None


def is_proxy_available() -> bool:
    return _proxy_available


def get_last_used() -> float:
    return _last_used


def get_http_proxy_url() -> str:
    return f"http://127.0.0.1:{config.XRAY_HTTP_PORT}"


def get_browser_proxy() -> str:
    """Return the proxy string SeleniumBase should use for Chromium."""
    if config.XRAY_PROXY_SCHEME == "socks5":
        return f"socks5://127.0.0.1:{config.XRAY_SOCKS_PORT}"
    # SeleniumBase treats a bare host:port as an HTTP proxy.
    return f"127.0.0.1:{config.XRAY_HTTP_PORT}"


# ---------------------------------------------------------------------------
# VLESS parsing / config generation
# ---------------------------------------------------------------------------
def get_xray_download_url():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        arch = "linux-arm64-v8a" if "aarch64" in machine or "arm64" in machine else "linux-64"
    elif system == "windows":
        arch = "windows-64"
    elif system == "darwin":
        arch = "macos-arm64-v8a" if "arm" in machine else "macos-64"
    else:
        arch = "linux-64"
    filename = f"Xray-{arch}.zip"
    return (
        f"https://github.com/XTLS/Xray-core/releases/download/"
        f"v{config.XRAY_VERSION}/{filename}",
        filename,
    )


def parse_vless_url(vless_url: str):
    """Parse a vless:// URL into the fields needed by the Xray config."""
    try:
        if not vless_url.startswith("vless://"):
            return None
        url = vless_url[8:]
        fragment = ""
        if "#" in url:
            url, fragment = url.rsplit("#", 1)
            fragment = unquote(fragment)
        if "@" not in url:
            return None
        uuid, rest = url.split("@", 1)
        if "?" in rest:
            host_port, query_string = rest.split("?", 1)
        else:
            host_port = rest
            query_string = ""
        host, port = host_port.rsplit(":", 1)
        params = parse_qs(query_string)
        return {
            "uuid": uuid,
            "host": host,
            "port": int(port),
            "name": fragment or "Unknown",
            "security": params.get("security", ["none"])[0],
            "type": params.get("type", ["tcp"])[0],
            "flow": params.get("flow", [""])[0],
            "sni": params.get("sni", [""])[0],
            "fp": params.get("fp", ["chrome"])[0],
            "pbk": params.get("pbk", [""])[0],
            "sid": params.get("sid", [""])[0],
            "spx": params.get("spx", [""])[0],
            "path": params.get("path", ["/"])[0],
            "host_header": params.get("host", [""])[0],
            "service_name": params.get("serviceName", [""])[0],
            "_raw": vless_url,
        }
    except Exception as exc:
        logger.debug("parse_vless_url failed: %s", exc)
        return None


def generate_xray_config(vless_config: dict) -> dict:
    security = (vless_config.get("security") or "none").lower()
    network = (vless_config.get("type") or "tcp").lower()

    stream_settings = {"network": network, "security": security}
    if security == "reality":
        stream_settings["realitySettings"] = {
            "show": False,
            "fingerprint": vless_config.get("fp", "chrome"),
            "serverName": vless_config.get("sni", ""),
            "publicKey": vless_config.get("pbk", ""),
            "shortId": vless_config.get("sid", ""),
            "spiderX": vless_config.get("spx", "") or "",
        }
    elif security == "tls":
        stream_settings["tlsSettings"] = {
            "serverName": vless_config.get("sni", ""),
            "fingerprint": vless_config.get("fp", "chrome"),
            "allowInsecure": False,
        }

    if network == "ws":
        stream_settings["wsSettings"] = {
            "path": vless_config.get("path", "/"),
            "headers": (
                {"Host": vless_config["host_header"]}
                if vless_config.get("host_header")
                else {}
            ),
        }
    elif network == "grpc":
        stream_settings["grpcSettings"] = {
            "serviceName": vless_config.get("service_name", "")
        }

    return {
        "log": {"loglevel": "warning"},
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
        "inbounds": [
            {
                "tag": "socks-in",
                "port": config.XRAY_SOCKS_PORT,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            },
            {
                "tag": "http-in",
                "port": config.XRAY_HTTP_PORT,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {},
            },
        ],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": vless_config["host"],
                            "port": vless_config["port"],
                            "users": [
                                {
                                    "id": vless_config["uuid"],
                                    "encryption": "none",
                                    "flow": vless_config.get("flow", ""),
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            },
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "outboundTag": "proxy", "port": "0-65535"}
            ],
        },
    }


# ---------------------------------------------------------------------------
# Download / process lifecycle
# ---------------------------------------------------------------------------
async def download_xray() -> bool:
    os.makedirs(config.XRAY_DIR, exist_ok=True)
    if os.path.exists(config.XRAY_BINARY):
        return True

    url, filename = get_xray_download_url()
    logger.info("Downloading Xray %s from %s", config.XRAY_VERSION, url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    logger.error("Xray download failed: HTTP %s", resp.status)
                    return False
                data = await resp.read()

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if member.endswith("xray") or member.endswith("xray.exe"):
                    with open(config.XRAY_BINARY, "wb") as out:
                        out.write(zf.read(member))
                    if platform.system() != "Windows":
                        os.chmod(config.XRAY_BINARY, 0o755)
                    logger.info("Xray binary ready at %s", config.XRAY_BINARY)
                    return True
        logger.error("Xray zip did not contain a binary")
        return False
    except Exception as exc:
        logger.error("Xray download error: %s", exc)
        return False


def _tail_log(path: str, limit: int = 1500) -> str:
    try:
        with open(path, "r", errors="ignore") as fh:
            return fh.read()[-limit:]
    except Exception:
        return ""


async def start_xray(vless_config: dict) -> bool:
    global _process, _proxy_available
    stop_xray()

    os.makedirs(config.XRAY_DIR, exist_ok=True)
    with open(config.XRAY_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(generate_xray_config(vless_config), fh, indent=2)

    logger.info(
        "Starting Xray: %s (%s:%s)",
        vless_config.get("name", "?"),
        vless_config["host"],
        vless_config["port"],
    )

    log_file = open(config.XRAY_LOG_PATH, "ab")
    try:
        _process = subprocess.Popen(
            [config.XRAY_BINARY, "run", "-c", config.XRAY_CONFIG_PATH],
            stdout=log_file,
            stderr=log_file,
            cwd=config.XRAY_DIR,
        )
    except Exception as exc:
        logger.error("Xray start error: %s", exc)
        _process = None
        return False
    finally:
        # Child keeps its own duplicate of the handle.
        log_file.close()

    # Give it a moment and make sure it did not exit immediately.
    for _ in range(10):
        await asyncio.sleep(0.5)
        if _process is not None and _process.poll() is not None:
            logger.error("Xray exited during startup:\n%s", _tail_log(config.XRAY_LOG_PATH))
            _process = None
            _proxy_available = False
            return False

    return True


def stop_xray():
    global _process, _proxy_available
    if _process is not None:
        logger.info("Stopping Xray")
        try:
            _process.terminate()
            try:
                _process.wait(timeout=5)
            except Exception:
                _process.kill()
        except Exception:
            pass
    _process = None
    _proxy_available = False


async def test_proxy_connection(retries: int = 2) -> bool:
    proxy_url = get_http_proxy_url()
    last_error = ""
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.ipify.org?format=json",
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info("Xray exit IP: %s", data.get("ip"))
                        return True
                    last_error = f"HTTP {resp.status}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        # Fallback check against the target site.
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://socpublic.com/",
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status < 500:
                        logger.info("Xray proxy reachable (socpublic HTTP %s)", resp.status)
                        return True
                    last_error = f"socpublic HTTP {resp.status}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries - 1:
            await asyncio.sleep(2)

    logger.warning("Xray proxy test failed: %s", last_error)
    return False


# ---------------------------------------------------------------------------
# Subscription support (optional)
# ---------------------------------------------------------------------------
async def fetch_subscription_config() -> str:
    """Fetch XRAY_SUBSCRIPTION_URL and pick a vless:// line."""
    if not config.XRAY_SUBSCRIPTION_URL:
        return ""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                config.XRAY_SUBSCRIPTION_URL,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.error("Subscription fetch failed: HTTP %s", resp.status)
                    return ""
                text = (await resp.text(errors="ignore")).strip()
    except Exception as exc:
        logger.error("Subscription fetch error: %s", exc)
        return ""

    # Many providers return base64-encoded lists.
    if "vless://" not in text:
        try:
            decoded = base64.b64decode(text + "===").decode("utf-8", errors="ignore")
            if "vless://" in decoded:
                text = decoded
        except Exception:
            pass

    links = [line.strip() for line in text.splitlines() if line.strip().startswith("vless://")]
    if not links:
        logger.error("Subscription contained no vless:// links")
        return ""

    preferred = config.XRAY_PREFERRED_SERVER.lower()
    if preferred:
        for link in links:
            name = unquote(link.rsplit("#", 1)[-1]).lower() if "#" in link else ""
            if preferred in name:
                logger.info("Selected subscription server: %s", name)
                return link

    logger.info("Using first subscription server")
    return links[0]


async def resolve_vless_config() -> str:
    global _cached_vless
    if config.XRAY_VLESS_CONFIG:
        return config.XRAY_VLESS_CONFIG
    if _cached_vless:
        return _cached_vless
    _cached_vless = await fetch_subscription_config()
    return _cached_vless


# ---------------------------------------------------------------------------
# On-demand API
# ---------------------------------------------------------------------------
async def ensure_running() -> bool:
    """Start the tunnel if needed. Safe to call before every solve."""
    global _proxy_available, _last_used

    async with _lock:
        if is_running() and _proxy_available:
            _last_used = time.time()
            return True

        if is_running():
            stop_xray()

        if not await download_xray():
            return False

        vless = await resolve_vless_config()
        if not vless:
            logger.error("No XRAY_VLESS_CONFIG or subscription available")
            return False

        parsed = parse_vless_url(vless)
        if not parsed:
            logger.error("Failed to parse VLESS config")
            return False

        if not await start_xray(parsed):
            return False

        ok = await test_proxy_connection()
        _proxy_available = ok
        _last_used = time.time() if ok else 0.0
        if ok:
            logger.info("Xray tunnel is ready")
        return ok


async def stop_if_idle(busy_check=None) -> None:
    if busy_check and busy_check():
        return
    if is_running() and _last_used and time.time() - _last_used > config.XRAY_IDLE_TIMEOUT:
        logger.info("Xray idle for %ss - stopping tunnel", config.XRAY_IDLE_TIMEOUT)
        stop_xray()


async def idle_monitor(busy_check=None) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await stop_if_idle(busy_check)
        except Exception:
            logger.exception("Xray idle monitor error")


def status() -> dict:
    return {
        "running": is_running(),
        "proxy_available": _proxy_available,
        "last_used": _last_used,
        "http_proxy": get_http_proxy_url(),
        "browser_proxy": get_browser_proxy(),
    }
