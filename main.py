"""
Turnstile solver microservice using FastAPI and SeleniumBase CDP mode.

Strategy (inspired by Michael Mintz / SeleniumBase creator):
  1. Launch a stealth browser via SeleniumBase UC+CDP mode (no raw WebDriver)
  2. Navigate to https://tma.foxigrow.com
  3. Inject the Cloudflare Turnstile widget for our sitekey
  4. Use sb.solve_captcha() to auto-solve the Turnstile challenge
  5. Extract the token from window.__turnstileToken callback
  6. Return the token — used by the caller for /captcha/verify API
"""

import os
import time
import logging
import traceback
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Turnstile Solver API")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SITEKEY = "0x4AAAAAADuXG2nt8DMgL_NF"
PAGEURL = "https://tma.foxigrow.com"

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/tmp")


def parse_proxy(raw_proxy: str) -> Optional[str]:
    """Parse proxy from HOST:PORT:USER:PASS to USER:PASS@HOST:PORT format."""
    if not raw_proxy:
        return None
    try:
        parts = raw_proxy.strip().split(':')
        if len(parts) == 4:
            host, port, user, password = parts
            return f"{user}:{password}@{host}:{port}"
        else:
            logger.warning(f"Unsupported proxy format. Expected HOST:PORT:USER:PASS, got {len(parts)} parts.")
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

    var container = document.createElement('div');
    container.id = '__cf_turnstile_solver';
    container.style.cssText = 'position:fixed;top:0;left:0;z-index:2147483647;background:white;padding:5px;';
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
        }} catch(e) {{
            window.__turnstileError = 'render_failed: ' + String(e);
        }}
    }};

    document.head.appendChild(script);
}})();
"""


def solve_turnstile(proxy_str: Optional[str] = None) -> Optional[str]:
    """Navigate to target page, inject Turnstile, use solve_captcha(), return token."""
    from seleniumbase import SB
    import platform

    is_linux = platform.system() == "Linux"
    logger.info(f"Launching SeleniumBase UC+CDP browser (Linux={is_linux})...")

    try:
        # UC mode auto-activates CDP mode on sb.open()
        # xvfb=True on Linux creates a virtual display (appears headed to Cloudflare)
        with SB(uc=True, proxy=proxy_str, xvfb=is_linux) as sb:
            logger.info(f"Navigating to {PAGEURL}...")
            sb.open(PAGEURL)
            sb.wait_for_element("body", timeout=30)

            logger.info("Injecting Turnstile widget...")
            try:
                sb.execute_script(INJECT_JS)
            except Exception as e:
                logger.error(f"Failed to inject widget: {e}")
                return None

            # Wait for the Turnstile API script to load
            logger.info("Waiting for Turnstile API to load...")
            api_loaded = False
            for attempt in range(20):
                time.sleep(0.5)
                try:
                    loaded = sb.execute_script("typeof window.turnstile !== 'undefined'")
                    if loaded:
                        api_loaded = True
                        logger.info(f"Turnstile API loaded after {(attempt + 1) * 0.5:.1f}s")
                        break
                except Exception:
                    pass

            if not api_loaded:
                logger.error("Turnstile API never loaded!")
                return None

            # Give widget time to render the iframe
            time.sleep(3)

            # Check if token was auto-granted (invisible mode on trusted environments)
            token = sb.execute_script("window.__turnstileToken || null")
            if token and len(str(token)) > 10:
                logger.info("Token auto-granted (invisible solve)!")
                return token

            # Use SeleniumBase's built-in captcha solver (clicks Turnstile checkbox)
            logger.info("Attempting sb.solve_captcha()...")
            try:
                sb.solve_captcha()
                logger.info("solve_captcha() completed")
            except Exception as e:
                logger.warning(f"solve_captcha() exception: {e}")
                # Fallback: try uc_gui_click_captcha
                try:
                    sb.uc_gui_click_captcha()
                    logger.info("uc_gui_click_captcha() completed")
                except Exception as e2:
                    logger.warning(f"uc_gui_click_captcha() also failed: {e2}")

            # Poll for the token (up to 30s)
            logger.info("Waiting for Turnstile token (up to 30s)...")
            for i in range(60):
                time.sleep(0.5)
                try:
                    token = sb.execute_script("window.__turnstileToken || null")
                    if token and len(str(token)) > 10:
                        logger.info(f"SUCCESS! Token obtained in {(i + 1) * 0.5:.1f}s")
                        return token

                    error = sb.execute_script("window.__turnstileError || null")
                    if error:
                        logger.error(f"Turnstile error: {error}")
                except Exception:
                    pass

            logger.error("Timeout: No Turnstile token after 30s")
            return None

    except Exception as e:
        logger.error(f"Error during solve: {e}")
        logger.error(traceback.format_exc())
        return None


@app.get("/")
def root():
    return {"status": "ok", "service": "Turnstile Solver API"}


os.makedirs(SCREENSHOT_DIR, exist_ok=True)


@app.get("/getToken")
def get_token():
    """Solve a Turnstile captcha and return the token."""
    try:
        raw_proxy = os.environ.get("PROXY_URL", "")
        proxy_config = parse_proxy(raw_proxy)

        if proxy_config:
            logger.info("Using configured PROXY_URL")
        else:
            logger.warning("NO PROXY_URL CONFIGURED!")

        start_time = time.time()
        token = solve_turnstile(proxy_config)
        elapsed = time.time() - start_time

        if token:
            return JSONResponse(content={
                "success": True,
                "token": token,
                "elapsed_s": round(elapsed, 2)
            })
        else:
            return JSONResponse(
                content={"success": False, "error": "Failed to solve Turnstile"},
                status_code=503
            )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Unhandled exception: {tb}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
