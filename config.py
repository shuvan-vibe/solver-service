import os
import platform
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- API / SERVICE ---
VERSION = "1.0.0"
PORT = _int("PORT", 8080)
SOLVER_API_KEY = os.getenv("SOLVER_API_KEY", "").strip()
SOLVER_MAX_RETRIES = _int("SOLVER_MAX_RETRIES", 2)
SOLVER_MAX_WAIT = _int("SOLVER_MAX_WAIT", 180)
SOLVER_HEADLESS = _bool("SOLVER_HEADLESS", False)
SOLVER_PUBLIC_URL = os.getenv("SOLVER_PUBLIC_URL", "").strip()

# --- TARGETS ---
DEFAULT_SITEKEY = os.getenv("DEFAULT_SITEKEY", "0x4AAAAAADuXG2nt8DMgL_NF")
DEFAULT_PAGEURL = os.getenv("DEFAULT_PAGEURL", "https://tma.foxigrow.com")
SOCPUBLIC_SITEKEY = os.getenv("SOCPUBLIC_SITEKEY", "0x4AAAAAAAufaStIBeFnOKze")
SOCPUBLIC_PAGEURL = os.getenv(
    "SOCPUBLIC_PAGEURL",
    "https://socpublic.com/auth_login.html?captcha_type=Turnstile",
)

# --- NETWORK ---
PROXY_URL = (os.getenv("PROXY_URL") or os.getenv("PROXY_URI") or "").strip()
XRAY_VLESS_CONFIG = os.getenv("XRAY_VLESS_CONFIG", "").strip()
XRAY_SUBSCRIPTION_URL = os.getenv("XRAY_SUBSCRIPTION_URL", "").strip()
XRAY_PREFERRED_SERVER = os.getenv("XRAY_PREFERRED_SERVER", "").strip()

NETWORK_MODE = os.getenv("SOLVER_NETWORK_MODE", "").strip().lower()
if not NETWORK_MODE:
    if XRAY_VLESS_CONFIG or XRAY_SUBSCRIPTION_URL:
        NETWORK_MODE = "xray"
    elif PROXY_URL:
        NETWORK_MODE = "proxy"
    else:
        NETWORK_MODE = "direct"

# --- XRAY ---
XRAY_VERSION = os.getenv("XRAY_VERSION", "1.8.7")
_DEFAULT_XRAY_DIR = (
    os.path.join(os.environ.get("TEMP", "."), "xray")
    if platform.system() == "Windows"
    else "/tmp/xray"
)
XRAY_DIR = os.getenv("XRAY_DIR", _DEFAULT_XRAY_DIR)
XRAY_BINARY = os.getenv(
    "XRAY_BINARY",
    os.path.join(XRAY_DIR, "xray.exe" if platform.system() == "Windows" else "xray"),
)
XRAY_CONFIG_PATH = os.path.join(XRAY_DIR, "config.json")
XRAY_LOG_PATH = os.path.join(XRAY_DIR, "xray.log")
XRAY_SOCKS_PORT = _int("XRAY_SOCKS_PORT", 10808)
XRAY_HTTP_PORT = _int("XRAY_HTTP_PORT", 10809)
XRAY_IDLE_TIMEOUT = _int("XRAY_IDLE_TIMEOUT", 600)
XRAY_START_TIMEOUT = _int("XRAY_START_TIMEOUT", 25)
XRAY_PROXY_SCHEME = os.getenv("XRAY_PROXY_SCHEME", "http").strip().lower()

# --- BROWSER ---
BROWSER_IDLE_TIMEOUT = _int("BROWSER_IDLE_TIMEOUT", 900)
BROWSER_MAX_SOLVES = _int("BROWSER_MAX_SOLVES", 250)
BROWSER_MAX_FAILURES = _int("BROWSER_MAX_FAILURES", 3)

# --- MONGO HEARTBEAT ---
MONGO_URI = (os.getenv("MONGO_URI") or os.getenv("MONGO_URL") or "").strip()
MONGO_DB = os.getenv("MONGO_DB", "socpublic_db")
SOLVER_STATUS_COLLECTION = os.getenv("SOLVER_STATUS_COLLECTION", "solver_status")
HEARTBEAT_INTERVAL = _int("HEARTBEAT_INTERVAL", 60)
