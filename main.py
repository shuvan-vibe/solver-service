"""Self-hosted Turnstile solver microservice.

FastAPI + SeleniumBase UC browser.  The Xray/VLESS tunnel is started on demand
and stopped again when idle, so the service can sit behind a Cloudflare tunnel
without burning CPU/RAM between logins.

Endpoints
---------
GET /                      - liveness (public)
GET /health                - detailed status (API key)
GET /getToken              - legacy foxigrow token (API key)
GET /getSocpublicToken     - Socpublic login token (API key)
GET /solve?sitekey=&pageurl= - generic token (API key)
GET /warmup                - pre-start Xray + browser (API key)
"""

import asyncio
import logging
import os
import platform
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp
import config
import xray_manager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Turnstile Solver API", version=config.VERSION)

# One browser, one solve at a time.
solve_lock = threading.Lock()

service_started_at = time.time()
is_shutting_down = False
global_sb_context = None
global_sb = None
solve_count = 0
consecutive_failures = 0
last_solve_at = 0.0
last_solve_success = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def check_api_key(request: Request):
    if not config.SOLVER_API_KEY:
        return
    provided = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(provided, config.SOLVER_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------
def parse_proxy(raw_proxy: str) -> Optional[str]:
    """Accept USER:PASS@HOST:PORT or HOST:PORT:USER:PASS."""
    if not raw_proxy or not raw_proxy.strip():
        return None
    raw_proxy = raw_proxy.strip()
    if "@" in raw_proxy:
        return raw_proxy
    try:
        parts = raw_proxy.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            return f"{user}:{password}@{host}:{port}"
        logger.warning("Unsupported PROXY_URL format: expected 4 parts, got %s", len(parts))
        return None
    except Exception as exc:
        logger.error("Failed to parse proxy: %s", exc)
        return None


def _get_proxy_str() -> Optional[str]:
    if config.NETWORK_MODE == "xray":
        if not xray_manager.is_proxy_available():
            raise RuntimeError("Xray proxy is not available")
        return xray_manager.get_browser_proxy()
    if config.NETWORK_MODE == "proxy":
        return parse_proxy(config.PROXY_URL)
    return None


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------
def browser_alive() -> bool:
    if global_sb is None:
        return False
    try:
        _ = global_sb.driver.title
        return True
    except Exception:
        return False


def close_browser():
    global global_sb_context, global_sb, solve_count, consecutive_failures
    if global_sb_context is not None:
        try:
            global_sb_context.__exit__(None, None, None)
        except Exception:
            pass
    global_sb = None
    global_sb_context = None
    solve_count = 0
    consecutive_failures = 0


def _launch_browser(proxy_str: Optional[str], pageurl: str):
    global global_sb_context, global_sb, solve_count, consecutive_failures

    from seleniumbase import SB

    is_linux = platform.system() == "Linux"
    is_docker = os.path.exists("/.dockerenv") or bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
    )
    docker_args = (
        "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" if is_docker else None
    )

    kwargs = dict(
        uc=True,
        proxy=proxy_str,
        xvfb=is_linux,
        headless=config.SOLVER_HEADLESS,
        use_chromium=True,
        ad_block=True,
        locale_code="en",
    )
    if docker_args:
        kwargs["chromium_arg"] = docker_args

    logger.info(
        "Launching SeleniumBase UC browser (proxy=%s)",
        "configured" if proxy_str else "none",
    )
    ctx = SB(**kwargs)
    sb = ctx.__enter__()

    try:
        sb.driver.set_page_load_timeout(45)
        logger.info("Opening %s...", pageurl)
        sb.open(pageurl)
        time.sleep(3)
    except Exception:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass
        raise

    global_sb_context = ctx
    global_sb = sb
    solve_count = 0
    consecutive_failures = 0
    return sb


def _ensure_browser(proxy_str: Optional[str], pageurl: str):
    global global_sb, solve_count, consecutive_failures

    alive = False
    if (
        global_sb is not None
        and solve_count < config.BROWSER_MAX_SOLVES
        and consecutive_failures < config.BROWSER_MAX_FAILURES
    ):
        try:
            _ = global_sb.driver.title
            alive = True
        except Exception:
            alive = False

    if not alive:
        logger.info(
            "Browser state (count=%s, failures=%s). Launching fresh browser.",
            solve_count,
            consecutive_failures,
        )
        close_browser()
        _launch_browser(proxy_str, pageurl)
    else:
        logger.info("Reusing browser (count=%s). Refreshing page...", solve_count)
        global_sb.driver.set_page_load_timeout(45)
        global_sb.open(pageurl)
        time.sleep(2)

    return global_sb


def stabilize_browser():
    """Reconnect CDP after uc_gui_click_captcha() disconnects it."""
    global global_sb
    if global_sb is None:
        return
    try:
        try:
            global_sb.reconnect(0.5)
        except Exception:
            pass
        global_sb.driver.get("about:blank")
        time.sleep(0.3)
        _ = global_sb.driver.title
        logger.info("Browser stabilized for reuse.")
    except Exception as exc:
        logger.warning("Could not stabilize browser: %s", exc)


def _finish_success(token: str) -> str:
    global consecutive_failures, last_solve_success
    consecutive_failures = 0
    last_solve_success = True
    stabilize_browser()
    return token


def _finish_failure():
    global consecutive_failures, last_solve_success
    consecutive_failures = config.BROWSER_MAX_FAILURES
    last_solve_success = False


# ---------------------------------------------------------------------------
# Turnstile injection / solve
# ---------------------------------------------------------------------------
TOKEN_JS = """
(function(){
    // 1. Our injected widget's callback.
    if (window.__turnstileToken) return window.__turnstileToken;
    // 2. The page's own Cloudflare widget writes the token here.
    var inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
    for (var i = 0; i < inputs.length; i++) {
        if (inputs[i].value) return inputs[i].value;
    }
    // 3. Ask the Turnstile API for any rendered widget's response.
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
        try {
            var resp = window.turnstile.getResponse();
            if (resp) return resp;
        } catch(e) {}
    }
    return null;
})()
"""

PAGE_STATE_JS = """
(function(){
    var inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
    var values = [];
    for (var i = 0; i < inputs.length; i++) values.push(!!inputs[i].value);
    var iframes = [];
    var frames = document.querySelectorAll('iframe');
    for (var j = 0; j < frames.length; j++) {
        iframes.push({
            src: (frames[j].src || '').slice(0, 120),
            visible: !!(frames[j].offsetWidth || frames[j].offsetHeight)
        });
    }
    return {
        url: location.href,
        title: document.title,
        turnstileExists: typeof window.turnstile !== 'undefined',
        injected: !!window.__turnstileInjected,
        injectedToken: window.__turnstileToken || null,
        injectedError: window.__turnstileError || null,
        cfWidgets: document.querySelectorAll('.cf-turnstile').length,
        responseInputs: inputs.length,
        responseInputsFilled: values,
        iframes: iframes
    };
})()
"""


def _page_has_widget(sb) -> bool:
    """True when the target page renders its own Turnstile widget."""
    try:
        return bool(
            sb.execute_script(
                """(function(){
                    return !!document.querySelector('.cf-turnstile')
                        || !!document.querySelector('input[name="cf-turnstile-response"]');
                })()"""
            )
        )
    except Exception:
        return False


def _capture_diagnostics(sb, tag: str = "timeout") -> None:
    """Save a screenshot and dump page state so failures are debuggable."""
    try:
        path = os.path.join(os.getcwd(), f"{tag}.png")
        sb.save_screenshot(path)
        logger.info("Diagnostics screenshot: %s", path)
    except Exception as exc:
        logger.warning("Diagnostics screenshot failed: %s", exc)

    try:
        logger.info("Page state: %s", sb.execute_script(PAGE_STATE_JS))
    except Exception as exc:
        logger.warning("Page state dump failed: %s", exc)

    try:
        logger.info("Current URL: %s | title: %s", sb.get_current_url(), sb.get_page_title())
    except Exception:
        pass


def get_inject_js(sitekey: str) -> str:
    return f"""
(function() {{
    var existing = document.getElementById('__cf_turnstile_solver');
    if (window.__turnstileInjected && existing) return;
    window.__turnstileInjected = true;
    window.__turnstileToken = null;
    window.__turnstileError = null;
    window.__turnstileWidgetId = null;
    if (existing) existing.remove();

    var container = document.createElement('div');
    container.id = '__cf_turnstile_solver';
    container.className = 'cf-turnstile-wrapper cf-turnstile';
    container.style.cssText = 'position:fixed;top:50px;left:50px;z-index:2147483647;background:white;padding:10px;';
    document.body.appendChild(container);

    window.__onTurnstileLoad = function() {{
        try {{
            if (window.__turnstileWidgetId !== null) return;
            var widgetId = window.turnstile.render('#__cf_turnstile_solver', {{
                sitekey: '{sitekey}',
                callback: function(token) {{
                    window.__turnstileToken = token;
                    console.log('[Solver] Token received');
                }},
                'error-callback': function(code) {{
                    window.__turnstileError = String(code);
                    console.log('[Solver] Error: ' + code);
                }},
                'expired-callback': function() {{
                    window.__turnstileToken = null;
                }}
            }});
            window.__turnstileWidgetId = widgetId;
        }} catch(e) {{
            window.__turnstileError = 'render_failed: ' + String(e);
        }}
    }};

    if (typeof window.turnstile !== 'undefined') {{
        window.__onTurnstileLoad();
        return;
    }}

    var script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__onTurnstileLoad';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);
}})();
"""


def solve_turnstile(sitekey: str, pageurl: str) -> Optional[str]:
    """Navigate to target page, inject Turnstile, auto-solve, return token."""
    global global_sb, solve_count

    proxy_str = _get_proxy_str()

    try:
        sb = _ensure_browser(proxy_str, pageurl)
        solve_count += 1

        sb.wait_for_element("body", timeout=30)

        logger.info("Injecting Turnstile widget (sitekey=%s)...", sitekey)
        try:
            sb.execute_script(get_inject_js(sitekey))
        except Exception as exc:
            logger.error("Failed to inject widget: %s", exc)
            _finish_failure()
            return None

        logger.info("Waiting for Turnstile API to load...")
        api_loaded = False
        for attempt in range(40):  # up to 20s
            time.sleep(0.5)
            try:
                status = sb.execute_script(
                    """(function(){
                        var pageToken = null;
                        var inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
                        for (var i = 0; i < inputs.length; i++) {
                            if (inputs[i].value) { pageToken = inputs[i].value; break; }
                        }
                        if (!pageToken && window.turnstile && typeof window.turnstile.getResponse === 'function') {
                            try { pageToken = window.turnstile.getResponse(); } catch(e) {}
                        }
                        return {
                            turnstileExists: typeof window.turnstile !== 'undefined',
                            injected: !!window.__turnstileInjected,
                            token: window.__turnstileToken || pageToken || null,
                            error: window.__turnstileError || null,
                            cfWidgets: document.querySelectorAll('.cf-turnstile').length,
                            iframes: document.querySelectorAll('iframe').length
                        };
                    })()"""
                )
                if status and status.get("token") and len(str(status["token"])) > 10:
                    logger.info("Token auto-granted during API load wait!")
                    return _finish_success(status["token"])
                if status and status.get("turnstileExists"):
                    api_loaded = True
                    logger.info("Turnstile API loaded after %.1fs", (attempt + 1) * 0.5)
                    time.sleep(3)  # let the iframe render
                    break
                if attempt == 10 and status and not status.get("injected"):
                    logger.warning("Injection lost - re-injecting Turnstile widget...")
                    sb.execute_script(get_inject_js(sitekey))
            except Exception:
                pass

        if not api_loaded:
            logger.warning("Turnstile API slow to load - continuing anyway...")

        token = sb.execute_script(TOKEN_JS)
        if token and len(str(token)) > 10:
            logger.info("Token auto-granted (invisible solve)!")
            return _finish_success(token)

        # Strategy 1: SeleniumBase's UC GUI click (best on Linux/Xvfb).
        logger.info("Strategy 1: Attempting uc_gui_click_captcha()...")
        try:
            sb.uc_gui_click_captcha()
            logger.info("uc_gui_click_captcha() completed")
            time.sleep(2)
            token = sb.execute_script(TOKEN_JS)
            if token and len(str(token)) > 10:
                logger.info("Token obtained via uc_gui_click_captcha!")
                return _finish_success(token)
        except Exception as exc:
            logger.warning("uc_gui_click_captcha() failed: %s", exc)

        # Strategy 2: direct iframe click.
        logger.info("Strategy 2: Attempting uc_click on iframe...")
        try:
            sb.uc_click("iframe")
            logger.info("uc_click completed")
        except Exception as exc:
            logger.warning("uc_click failed: %s", exc)

        logger.info("Waiting for Turnstile token (up to 30s)...")
        for i in range(60):
            time.sleep(0.5)
            try:
                token = sb.execute_script(TOKEN_JS)
                if token and len(str(token)) > 10:
                    logger.info("SUCCESS! Token obtained in %.1fs", (i + 1) * 0.5)
                    return _finish_success(token)

                error = sb.execute_script("window.__turnstileError || null")
                if error:
                    logger.warning("Turnstile widget error: %s", error)
                    sb.execute_script(
                        """(function(){
                            window.__turnstileError = null;
                            if (window.turnstile && window.__turnstileWidgetId !== null) {
                                try { window.turnstile.reset(window.__turnstileWidgetId); } catch(e) {}
                            }
                        })()"""
                    )

                if i == 20:
                    logger.info("Retrying click on Turnstile widget...")
                    try:
                        sb.uc_gui_click_captcha()
                    except Exception:
                        try:
                            sb.uc_click("#__cf_turnstile_solver")
                        except Exception:
                            pass
            except Exception:
                pass

        logger.error("Timeout: No Turnstile token after 30s")
        raise Exception("Turnstile timeout")

    except Exception as exc:
        logger.error("Error during solve: %s", exc)
        logger.info("Forcing browser restart on next attempt to recover.")
        _finish_failure()
        return None


# ---------------------------------------------------------------------------
# Solve orchestration
# ---------------------------------------------------------------------------
class SolveBusy(Exception):
    pass


def _solve_with_lock(sitekey: str, pageurl: str) -> dict:
    global last_solve_at

    if not solve_lock.acquire(timeout=config.SOLVER_MAX_WAIT):
        raise SolveBusy()

    try:
        last_solve_at = time.time()
        started = time.time()

        for attempt in range(1, config.SOLVER_MAX_RETRIES + 1):
            try:
                logger.info(
                    "Solve attempt %s/%s for %s",
                    attempt,
                    config.SOLVER_MAX_RETRIES,
                    pageurl,
                )
                token = solve_turnstile(sitekey, pageurl)
                if token:
                    return {
                        "success": True,
                        "token": token,
                        "elapsed_s": round(time.time() - started, 2),
                        "attempts": attempt,
                    }
                logger.warning("Attempt %s failed", attempt)
            except Exception as exc:
                logger.error("Attempt %s error: %s", attempt, exc)

        return {
            "success": False,
            "error": "Failed after all retries",
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        solve_lock.release()


def _warmup_browser(pageurl: str) -> None:
    if not solve_lock.acquire(timeout=config.SOLVER_MAX_WAIT):
        raise SolveBusy()
    try:
        _ensure_browser(_get_proxy_str(), pageurl)
    finally:
        solve_lock.release()


async def _solve_common(sitekey: str, pageurl: str, label: str):
    if config.NETWORK_MODE == "xray":
        ok = await xray_manager.ensure_running()
        if not ok:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error": "Xray tunnel unavailable"},
            )

    try:
        result = await run_in_threadpool(_solve_with_lock, sitekey, pageurl)
    except SolveBusy:
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Solver busy, try again later"},
        )

    logger.info(
        "%s solve finished: success=%s elapsed=%ss",
        label,
        result.get("success"),
        result.get("elapsed_s"),
    )
    return JSONResponse(
        status_code=200 if result.get("success") else 503,
        content=result,
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
def _write_heartbeat():
    from pymongo import MongoClient

    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[config.MONGO_DB]
    doc = {
        "healthy": True,
        "version": config.VERSION,
        "network_mode": config.NETWORK_MODE,
        "xray_running": xray_manager.is_running(),
        "browser_alive": browser_alive(),
        "updated_at": datetime.now(timezone.utc),
    }
    if config.SOLVER_PUBLIC_URL:
        doc["url"] = config.SOLVER_PUBLIC_URL
    db[config.SOLVER_STATUS_COLLECTION].update_one(
        {"_id": "active"}, {"$set": doc}, upsert=True
    )


def _mark_offline():
    from pymongo import MongoClient

    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[config.MONGO_DB]
    db[config.SOLVER_STATUS_COLLECTION].update_one(
        {"_id": "active"},
        {
            "$set": {
                "healthy": False,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


async def heartbeat_loop():
    if not config.MONGO_URI:
        return
    if not config.SOLVER_PUBLIC_URL:
        logger.warning(
            "MONGO_URI is set but SOLVER_PUBLIC_URL is missing - heartbeat disabled"
        )
        return
    while True:
        try:
            await asyncio.to_thread(_write_heartbeat)
        except Exception as exc:
            logger.warning("Heartbeat failed: %s", exc)
        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


async def browser_idle_monitor():
    while True:
        await asyncio.sleep(30)
        if not last_solve_at:
            continue
        if time.time() - last_solve_at < config.BROWSER_IDLE_TIMEOUT:
            continue
        if not solve_lock.acquire(blocking=False):
            continue
        try:
            if global_sb is not None:
                logger.info(
                    "Browser idle for %ss - closing browser",
                    config.BROWSER_IDLE_TIMEOUT,
                )
                await run_in_threadpool(close_browser)
        except Exception:
            logger.exception("Browser idle close error")
        finally:
            solve_lock.release()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Turnstile Solver API",
        "version": config.VERSION,
    }


@app.get("/health")
async def health(_=Depends(check_api_key)):
    return {
        "status": "ok",
        "version": config.VERSION,
        "network_mode": config.NETWORK_MODE,
        "xray": xray_manager.status(),
        "browser_alive": browser_alive(),
        "solve_count": solve_count,
        "consecutive_failures": consecutive_failures,
        "last_solve_at": last_solve_at,
        "last_solve_success": last_solve_success,
        "public_url": config.SOLVER_PUBLIC_URL or None,
    }


@app.get("/getToken")
async def get_token(_=Depends(check_api_key)):
    return await _solve_common(
        config.DEFAULT_SITEKEY, config.DEFAULT_PAGEURL, "default"
    )


@app.get("/getSocpublicToken")
async def get_socpublic_token(_=Depends(check_api_key)):
    return await _solve_common(
        config.SOCPUBLIC_SITEKEY, config.SOCPUBLIC_PAGEURL, "socpublic"
    )


@app.get("/solve")
async def solve(
    sitekey: str,
    pageurl: str,
    _=Depends(check_api_key),
):
    parsed = urlparse(pageurl)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="pageurl must be an http(s) URL")
    if len(pageurl) > 2048:
        raise HTTPException(status_code=400, detail="pageurl is too long")
    return await _solve_common(sitekey, pageurl, "generic")


@app.get("/warmup")
async def warmup(pageurl: Optional[str] = None, _=Depends(check_api_key)):
    if config.NETWORK_MODE == "xray":
        ok = await xray_manager.ensure_running()
        if not ok:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error": "Xray tunnel unavailable"},
            )

    target = pageurl or config.SOCPUBLIC_PAGEURL
    try:
        await run_in_threadpool(_warmup_browser, target)
        return {"success": True, "browser_alive": browser_alive()}
    except SolveBusy:
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Solver busy, try again later"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503, content={"success": False, "error": str(exc)}
        )


async def _submit_socpublic_login(username: str, password: str, token: str) -> dict:
    """Submit credentials and Turnstile token to Socpublic via active proxy/tunnel."""
    login_url = "https://socpublic.com/auth_login.html?act=enter"

    proxy_url = None
    if config.NETWORK_MODE == "xray":
        proxy_url = f"http://127.0.0.1:{config.XRAY_HTTP_PORT}"
    elif config.NETWORK_MODE == "proxy" and config.PROXY_URL:
        parsed = parse_proxy(config.PROXY_URL)
        if parsed:
            proxy_url = f"http://{parsed}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Referer": "https://socpublic.com/auth_login.html?captcha_type=Turnstile",
        "Origin": "https://socpublic.com",
    }
    data = {
        "name": username,
        "password": password,
        "captcha_type": "Turnstile",
        "cf-turnstile-response": token,
    }

    logger.info(
        "Submitting Socpublic login (user=%s) via proxy %s...",
        username,
        proxy_url or "direct",
    )
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        try:
            async with session.post(
                login_url,
                data=data,
                headers=headers,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=35),
            ) as resp:
                resp_status = resp.status
                resp_url = str(resp.url)
                resp_text = await resp.text(errors="ignore")
        except Exception as exc:
            logger.error("POST to login failed: %s", exc)
            return {"success": False, "error": f"Login POST failed: {exc}"}

        logger.info("Login response: status=%s, url=%s", resp_status, resp_url)
        if "login.html" in resp_url or "auth" in resp_url or "captcha" in resp_text.lower():
            alerts = re.findall(r'<div class="alert[^"]*">(.*?)</div>', resp_text, re.DOTALL)
            err_msg = alerts[0].strip() if alerts else "Redirected to auth/login"
            err_msg = re.sub(r"<[^>]+>", "", err_msg).strip()
            logger.error("Login rejected by Socpublic: %s", err_msg)
            return {"success": False, "error": f"Socpublic login rejected: {err_msg}"}

        cookie_parts = []
        for c in session.cookie_jar:
            cookie_parts.append(f"{c.key}={c.value}")

        if not cookie_parts:
            logger.error("No cookies received in response")
            return {"success": False, "error": "No cookies captured from login"}

        # Verify on /account/task.html
        verify_headers = dict(headers)
        try:
            async with session.get(
                "https://socpublic.com/account/task.html",
                headers=verify_headers,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as v_resp:
                v_url = str(v_resp.url)
                if "login.html" in v_url or "auth" in v_url:
                    return {"success": False, "error": "Verification failed: redirected to login"}
        except Exception as exc:
            logger.warning("Cookie verification error: %s (continuing)", exc)

        # Set pagination to 100
        try:
            async with session.get(
                "https://socpublic.com/account/task.html?page_limit=100&page=1",
                headers=verify_headers,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as limit_resp:
                if limit_resp.status == 200:
                    logger.info("Pagination limit set to 100.")
        except Exception:
            pass

        cookie_str = "; ".join(cookie_parts)
        return {"success": True, "cookie": cookie_str}


@app.api_route("/loginProxy", methods=["GET", "POST"])
async def login_proxy(request: Request, _=Depends(check_api_key)):
    """Solve Turnstile and log into Socpublic via proxy, saving cookie to DB."""
    user = request.query_params.get("username")
    pwd = request.query_params.get("password")
    if request.method == "POST":
        try:
            body_json = await request.json()
            if isinstance(body_json, dict):
                user = body_json.get("username") or user
                pwd = body_json.get("password") or pwd
        except Exception:
            pass

    if not user:
        user = config.SP_USERNAME_PROXY
    if not pwd:
        pwd = config.SP_PASSWORD_PROXY

    if not user or not pwd:
        raise HTTPException(
            status_code=400,
            detail="Missing proxy username or password (pass via query/body or set SP_USERNAME_PROXY/SP_PASSWORD_PROXY)",
        )

    if config.NETWORK_MODE == "xray":
        ok = await xray_manager.ensure_running()
        if not ok:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error": "Xray tunnel unavailable"},
            )

    # 1. Obtain Turnstile token using seleniumbase UC browser
    try:
        solve_result = await run_in_threadpool(
            _solve_with_lock, config.SOCPUBLIC_SITEKEY, config.SOCPUBLIC_PAGEURL
        )
    except SolveBusy:
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Solver busy, try again later"},
        )

    if not solve_result.get("success") or not solve_result.get("token"):
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": "Failed to obtain Turnstile token",
                "solve_result": solve_result,
            },
        )

    token = solve_result["token"]
    logger.info("Turnstile token acquired. Submitting proxy login...")

    # 2. Submit login POST via proxy
    login_result = await _submit_socpublic_login(user, pwd, token)
    if not login_result.get("success"):
        return JSONResponse(status_code=401, content=login_result)

    cookie_str = login_result["cookie"]

    # 3. Save to MongoDB
    saved_to_db = False
    if config.MONGO_URI:
        try:
            from pymongo import MongoClient

            client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
            db = client[config.MONGO_DB]
            now_utc = datetime.now(timezone.utc)
            db["session_cookies"].update_one(
                {"_id": "proxy"},
                {
                    "$set": {
                        "cookie": cookie_str,
                        "updated_at": now_utc,
                        "is_expired": False,
                        "source": "solver_loginProxy",
                    }
                },
                upsert=True,
            )
            db["bot_data"].update_one(
                {"_id": "reboot_counter"},
                {"$set": {"reboot_count_proxy": 0, "updated_at": now_utc}},
                upsert=True,
            )
            saved_to_db = True
            logger.info("Saved proxy cookie to MongoDB (session_cookies._id='proxy').")
        except Exception as db_exc:
            logger.warning("Failed to save proxy cookie to MongoDB: %s", db_exc)

    return {
        "success": True,
        "cookie": cookie_str,
        "saved_to_db": saved_to_db,
        "solve_elapsed_s": solve_result.get("elapsed_s"),
    }


async def _trigger_clean_exit(reason: str = "idle"):
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True
    logger.info("Initiating graceful auto-shutdown (reason: %s)...", reason)
    if config.MONGO_URI:
        try:
            await asyncio.to_thread(_mark_offline)
            logger.info("Marked solver offline in MongoDB.")
        except Exception as exc:
            logger.warning("Failed to mark offline in MongoDB: %s", exc)
    try:
        await run_in_threadpool(close_browser)
    except Exception:
        pass
    xray_manager.stop_xray()
    try:
        import subprocess
        subprocess.run(["pkill", "-f", "cloudflared"], check=False)
    except Exception:
        pass
    logger.info("Auto-shutdown complete. Exiting runner process cleanly.")
    os._exit(0)


async def auto_shutdown_loop():
    """Monitor runner activity and terminate when idle to save minutes and remain TOS-compliant."""
    if not config.AUTO_SHUTDOWN_ENABLED:
        logger.info("Auto-shutdown is disabled by configuration.")
        return
    logger.info(
        "Auto-shutdown monitor active: initial timeout=%ss, post-solve idle=%ss",
        config.AUTO_SHUTDOWN_INITIAL_TIMEOUT,
        config.AUTO_SHUTDOWN_IDLE_TIMEOUT,
    )
    while True:
        await asyncio.sleep(10)
        if is_shutting_down:
            break
        if solve_lock.locked():
            continue

        now = time.time()
        # Case 1: Runner started, but no requests arrived within initial timeout
        if solve_count == 0 and (now - service_started_at > config.AUTO_SHUTDOWN_INITIAL_TIMEOUT):
            await _trigger_clean_exit("initial_timeout_no_solves")
            break

        # Case 2: Solved at least once, and idle for post-solve timeout
        if solve_count > 0 and last_solve_at > 0 and (now - last_solve_at > config.AUTO_SHUTDOWN_IDLE_TIMEOUT):
            await _trigger_clean_exit("post_solve_idle")
            break


@app.post("/shutdown")
async def shutdown_endpoint(_=Depends(check_api_key)):
    """Explicitly trigger shutdown of solver runner."""
    async def _do():
        await asyncio.sleep(1)
        await _trigger_clean_exit("explicit_shutdown_api")
    asyncio.create_task(_do())
    return {"status": "shutting_down"}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    logger.info(
        "Solver service starting (version=%s, network_mode=%s)",
        config.VERSION,
        config.NETWORK_MODE,
    )
    if not config.SOLVER_API_KEY:
        logger.warning(
            "SOLVER_API_KEY is not set - API authentication is DISABLED (local dev only)"
        )
    if config.NETWORK_MODE == "xray":
        logger.info("Xray is on-demand and will start with the first solve request")
    asyncio.create_task(xray_manager.idle_monitor(lambda: solve_lock.locked()))
    asyncio.create_task(browser_idle_monitor())
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(auto_shutdown_loop())


@app.on_event("shutdown")
async def _shutdown():
    logger.info("Solver service shutting down")
    try:
        await run_in_threadpool(close_browser)
    except Exception:
        pass
    xray_manager.stop_xray()
    if config.MONGO_URI:
        try:
            await asyncio.to_thread(_mark_offline)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
