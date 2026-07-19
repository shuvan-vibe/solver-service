"""
Turnstile solver microservice using FastAPI and SeleniumBase UC mode.

Strategy:
  1. Launch a stealth browser via SeleniumBase UC mode with residential proxy
  2. Navigate to https://tma.foxigrow.com using uc_open_with_reconnect
  3. Inject the Cloudflare Turnstile widget for our sitekey
  4. Use uc_gui_click_captcha() to auto-solve the Turnstile challenge
  5. Extract the token from window.__turnstileToken callback
  6. Return the token — used by the caller for /captcha/verify API

Designed to run on GitHub Actions with Xvfb (managed by SeleniumBase) + residential proxy.
"""

import os
import time
import logging
import traceback
import threading
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Turnstile Solver API")
solve_lock = threading.Lock()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SITEKEY = "0x4AAAAAADuXG2nt8DMgL_NF"
# Use a non-existent page to load a lightweight 404 HTML (808 bytes)
# instead of the heavy homepage. This saves massive proxy bandwidth!
PAGEURL = "https://tma.foxigrow.com/404.html"


def parse_proxy(raw_proxy: str) -> Optional[str]:
    """
    Parse proxy string into SeleniumBase format (USER:PASS@HOST:PORT).
    
    Accepts:
      - USER:PASS@HOST:PORT  (already correct — pass through)
      - HOST:PORT:USER:PASS  (legacy format — rearrange)
    """
    if not raw_proxy or not raw_proxy.strip():
        return None
    
    raw_proxy = raw_proxy.strip()
    
    # Format 1: USER:PASS@HOST:PORT — already in SeleniumBase format
    if '@' in raw_proxy:
        logger.info("Proxy format: USER:PASS@HOST:PORT (direct)")
        return raw_proxy
    
    # Format 2: HOST:PORT:USER:PASS — legacy format, needs rearranging
    try:
        parts = raw_proxy.split(':')
        if len(parts) == 4:
            host, port, user, password = parts
            formatted = f"{user}:{password}@{host}:{port}"
            logger.info("Proxy format: HOST:PORT:USER:PASS (converted)")
            return formatted
        else:
            logger.warning(f"Unsupported proxy format ({len(parts)} parts). "
                          f"Expected USER:PASS@HOST:PORT or HOST:PORT:USER:PASS")
            return None
    except Exception as e:
        logger.error(f"Failed to parse proxy: {e}")
        return None


# JavaScript to inject a Turnstile widget and capture its token
INJECT_JS = f"""
(function() {{
    if (window.__turnstileInjected) return;
    window.__turnstileInjected = true;
    window.__turnstileToken = null;
    window.__turnstileError = null;
    window.__turnstileWidgetId = null;

    var container = document.createElement('div');
    container.id = '__cf_turnstile_solver';
    container.style.cssText = 'position:fixed;top:50px;left:50px;z-index:2147483647;background:white;padding:10px;';
    document.body.appendChild(container);

    var script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__onTurnstileLoad';
    script.async = true;
    script.defer = true;

    window.__onTurnstileLoad = function() {{
        try {{
            var widgetId = window.turnstile.render('#__cf_turnstile_solver', {{
                sitekey: '{SITEKEY}',
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

    document.head.appendChild(script);
}})();
"""


# === Persistent browser state (reuse across requests for speed) ===
global_sb_context = None
global_sb = None
solve_count = 0
consecutive_failures = 0


def solve_turnstile() -> Optional[str]:
    """Navigate to target page, inject Turnstile, auto-solve, return token."""
    from seleniumbase import SB
    import platform
    global global_sb_context, global_sb, solve_count, consecutive_failures

    raw_proxy = os.environ.get("PROXY_URL") or os.environ.get("PROXY_URI", "")
    proxy_str = parse_proxy(raw_proxy)
    is_linux = platform.system() == "Linux"

    if proxy_str:
        logger.info("Using residential proxy for stealth")
    else:
        logger.warning("NO PROXY CONFIGURED — will likely be detected on cloud servers!")

    try:
        # Restart browser if first time, used 100+ times, or failed 3+ times in a row
        if global_sb is None or solve_count >= 100 or consecutive_failures >= 3:
            logger.info(f"Browser state (count={solve_count}, failures={consecutive_failures}). Launching fresh browser.")
            consecutive_failures = 0
            if global_sb_context is not None:
                try:
                    global_sb_context.__exit__(None, None, None)
                except Exception:
                    pass
                global_sb = None
                global_sb_context = None
                time.sleep(1)

            logger.info("Launching SeleniumBase UC browser...")
            # xvfb=True on Linux: let SeleniumBase manage the virtual display
            # This is critical — SB's internal CAPTCHA solving (uc_gui_click_captcha)
            # requires SB to own the Xvfb lifecycle for proper coordination.
            # use_chromium=True: Chrome 137+ removed --load-extension; Chromium keeps it
            # (needed for proxy extension loading).
            #
            # Docker-specific flags (Railway, Render, etc.):
            #   --no-sandbox: required when running as root in containers
            #   --disable-dev-shm-usage: /dev/shm is only 64MB in Docker (Chrome needs more)
            #   --disable-gpu: no GPU available in containers
            is_docker = os.path.exists("/.dockerenv") or os.environ.get("RAILWAY_ENVIRONMENT")
            docker_args = (
                "--no-sandbox,--disable-dev-shm-usage,--disable-gpu"
                if is_docker else None
            )
            if is_docker:
                logger.info("Docker/Railway environment detected — adding container-safe flags")
            
            global_sb_context = SB(
                uc=True,
                proxy=proxy_str,
                xvfb=is_linux,
                headless=False,
                use_chromium=True,
                ad_block=True,
                locale_code="en",
                chromium_arg=docker_args,
            )
            global_sb = global_sb_context.__enter__()
            solve_count = 0

            logger.info(f"Opening {PAGEURL}...")
            global_sb.driver.set_page_load_timeout(45)
            global_sb.open(PAGEURL)
            time.sleep(3)  # Let page fully render before any interaction
        else:
            logger.info(f"Reusing existing browser (count={solve_count}). Refreshing page...")
            global_sb.driver.set_page_load_timeout(45)
            global_sb.open(PAGEURL)
            time.sleep(2)

        solve_count += 1
        sb = global_sb

        # Wait for the page body so we can inject the Turnstile container
        sb.wait_for_element("body", timeout=30)

        logger.info("Injecting Turnstile widget...")
        try:
            sb.execute_script(INJECT_JS)
        except Exception as e:
            logger.error(f"Failed to inject widget: {e}")
            consecutive_failures += 1
            return None

        # Wait for the Turnstile API script to load (async)
        logger.info("Waiting for Turnstile API to load...")
        api_loaded = False
        for attempt in range(40):  # Up to 20 seconds
            time.sleep(0.5)
            try:
                status = sb.execute_script("""(function(){
                    return {
                        turnstileExists: typeof window.turnstile !== 'undefined',
                        injected: !!window.__turnstileInjected,
                        token: window.__turnstileToken || null,
                        error: window.__turnstileError || null
                    };
                })()""")

                if status and status.get('turnstileExists'):
                    api_loaded = True
                    logger.info(f"Turnstile API loaded after {(attempt + 1) * 0.5:.1f}s")
                    time.sleep(3)  # Extra time for iframe to render
                    break
                if status and status.get('token'):
                    logger.info("Token auto-granted during API load wait!")
                    consecutive_failures = 0
                    return status['token']
                
                # At 5 seconds, check if inject was lost (page might have reloaded)
                if attempt == 10 and not status.get('injected'):
                    logger.warning("Injection lost — re-injecting Turnstile widget...")
                    sb.execute_script(INJECT_JS)
            except Exception:
                pass

        if not api_loaded:
            logger.warning("Turnstile API slow to load — continuing anyway...")

        # Check if token was auto-granted (invisible mode on trusted environments)
        token = sb.execute_script("window.__turnstileToken || null")
        if token and len(str(token)) > 10:
            logger.info("Token auto-granted (invisible solve)!")
            consecutive_failures = 0
            return token

        # === Try multiple solving strategies ===

        # Strategy 1: uc_gui_click_captcha (best for Turnstile on Linux with Xvfb)
        logger.info("Strategy 1: Attempting uc_gui_click_captcha()...")
        try:
            sb.uc_gui_click_captcha()
            logger.info("uc_gui_click_captcha() completed")
            time.sleep(2)
            token = sb.execute_script("window.__turnstileToken || null")
            if token and len(str(token)) > 10:
                logger.info("Token obtained via uc_gui_click_captcha!")
                consecutive_failures = 0
                return token
        except Exception as e:
            logger.warning(f"uc_gui_click_captcha() failed: {e}")

        # Strategy 2: uc_gui_click_captcha retry with longer wait
        logger.info("Strategy 2: Retrying uc_gui_click_captcha with extra wait...")
        try:
            time.sleep(3)  # Give Turnstile more time to render
            sb.uc_gui_click_captcha()
            logger.info("uc_gui_click_captcha() retry completed")
            time.sleep(3)
            token = sb.execute_script("window.__turnstileToken || null")
            if token and len(str(token)) > 10:
                logger.info("Token obtained via uc_gui_click_captcha retry!")
                consecutive_failures = 0
                return token
        except Exception as e:
            logger.warning(f"uc_gui_click_captcha() retry failed: {e}")

        # Strategy 3: Direct click on the Turnstile widget container
        logger.info("Strategy 3: Attempting uc_click on widget...")
        try:
            sb.uc_click("#__cf_turnstile_solver")
            logger.info("uc_click completed")
        except Exception as e:
            logger.warning(f"uc_click failed: {e}")

        # Poll for the token (up to 30s)
        logger.info("Waiting for Turnstile token (up to 30s)...")
        for i in range(60):
            time.sleep(0.5)
            try:
                token = sb.execute_script("window.__turnstileToken || null")
                if token and len(str(token)) > 10:
                    logger.info(f"SUCCESS! Token obtained in {(i + 1) * 0.5:.1f}s")
                    consecutive_failures = 0
                    return token

                error = sb.execute_script("window.__turnstileError || null")
                if error:
                    logger.warning(f"Turnstile widget error: {error}")
                    # Reset error and try to reset the widget
                    sb.execute_script("""
                        window.__turnstileError = null;
                        if (window.turnstile && window.__turnstileWidgetId !== null) {
                            try { window.turnstile.reset(window.__turnstileWidgetId); } catch(e) {}
                        }
                    """)

                # At 10 seconds, try clicking again
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

    except Exception as e:
        logger.error(f"Error during solve: {e}")
        logger.info("Forcing browser restart on next attempt to recover from error.")
        consecutive_failures = 3
        return None


@app.on_event("shutdown")
def shutdown_event():
    """Ensure the browser closes when the server shuts down."""
    global global_sb_context, global_sb
    if global_sb_context is not None:
        logger.info("Shutting down global browser instance...")
        try:
            global_sb_context.__exit__(None, None, None)
        except Exception:
            pass
        global_sb = None
        global_sb_context = None


@app.get("/")
def root():
    return {"status": "ok", "service": "Turnstile Solver API"}


@app.get("/getToken")
@app.get("/gettoken")
def get_token():
    """Solve a Turnstile captcha and return the token. Auto-retries up to 3 times."""
    with solve_lock:
        max_retries = 3
        start_time = time.time()

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Solve attempt {attempt}/{max_retries}...")
                token = solve_turnstile()

                if token:
                    elapsed = time.time() - start_time
                    return JSONResponse(content={
                        "success": True,
                        "token": token,
                        "elapsed_s": round(elapsed, 2),
                        "attempts": attempt
                    })
                else:
                    logger.warning(f"Attempt {attempt} failed, "
                                  f"{'retrying...' if attempt < max_retries else 'giving up.'}")
            except Exception as e:
                logger.error(f"Attempt {attempt} error: {e}")

        elapsed = time.time() - start_time
        return JSONResponse(
            content={"success": False, "error": "Failed after all retries", "elapsed_s": round(elapsed, 2)},
            status_code=503
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
