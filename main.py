"""
Turnstile solver microservice using FastAPI and SeleniumBase UC mode.

Strategy:
  1. Navigate to https://tma.foxigrow.com (let it load as Guest — that's fine)
  2. Inject the Cloudflare Turnstile widget for our sitekey
  3. The UC stealth browser auto-solves it (no human interaction needed)
  4. Extract the token from window.__turnstileToken callback
  5. Return the token — used by the caller for /captcha/verify API
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
        window.turnstile.render('#__cf_turnstile_solver', {{
            sitekey: '{SITEKEY}',
            callback: function(token) {{
                window.__turnstileToken = token;
                console.log('[Solver] Turnstile token received: ' + token.slice(0, 20) + '...');
            }},
            'error-callback': function(code) {{
                window.__turnstileError = String(code);
                console.log('[Solver] Turnstile error: ' + code);
            }},
            'expired-callback': function() {{
                window.__turnstileToken = null;
                console.log('[Solver] Turnstile token expired');
            }}
        }});
    }};

    document.head.appendChild(script);
    console.log('[Solver] Turnstile widget injected');
}})();
"""


def solve_turnstile(proxy_str: Optional[str] = None, headless: bool = True) -> Optional[str]:
    """Navigate to the target page, inject Turnstile, let UC browser auto-solve, return token."""
    from seleniumbase import SB

    logger.info("Launching SeleniumBase UC browser...")
    try:
        with SB(uc=True, proxy=proxy_str, headless=headless) as sb:
            logger.info(f"Navigating to {PAGEURL}...")
            sb.open(PAGEURL)

            # Wait for the page body to exist
            time.sleep(3)

            logger.info("Injecting Turnstile widget...")
            try:
                sb.execute_script(INJECT_JS)
            except Exception as e:
                logger.error(f"Failed to inject widget: {e}")
                return None

            # Take screenshot to verify injection
            try:
                sb.save_screenshot(os.path.join(SCREENSHOT_DIR, "screenshot_injected.png"))
                logger.info("Saved injection screenshot.")
            except Exception:
                pass

            time.sleep(2)
            logger.info("Attempting to click Turnstile CAPTCHA (in case it requires interaction)...")
            try:
                sb.uc_gui_click_captcha()
                logger.info("Clicked CAPTCHA checkbox.")
            except Exception as e:
                logger.info(f"Could not click CAPTCHA (maybe auto-solving or not present): {e}")

            # Poll for the token (auto-solved by stealth browser)
            logger.info("Waiting for Turnstile auto-solve (up to 30s)...")
            for i in range(60):
                time.sleep(0.5)
                try:
                    token = sb.execute_script("return window.__turnstileToken || null;")
                    if token and len(str(token)) > 10:
                        logger.info(f"SUCCESS! Token obtained in {(i + 1) * 0.5:.1f}s")
                        return token

                    error = sb.execute_script("return window.__turnstileError || null;")
                    if error:
                        logger.error(f"Turnstile widget reported error: {error}")
                        # Don't return None on error — reset and keep waiting
                        # The widget may retry automatically
                except Exception:
                    pass

            # Timeout — save diagnostic screenshot
            logger.error("Timeout: No Turnstile token after 30s")
            try:
                url = sb.get_current_url()
                title = sb.get_title()
                logger.info(f"URL: {url} | Title: {title}")
                sb.save_screenshot(os.path.join(SCREENSHOT_DIR, "screenshot_timeout.png"))
                logger.info("Saved timeout screenshot.")
            except Exception:
                pass
            return None

    except Exception as e:
        logger.error(f"Error during solve: {e}")
        logger.error(traceback.format_exc())
        return None


@app.get("/")
def root():
    return {"status": "ok", "service": "Turnstile Solver API"}


@app.get("/getToken")
def get_token():
    """Solve a Turnstile captcha and return the token."""
    import io
    log_capture_string = io.StringIO()
    ch = logging.StreamHandler(log_capture_string)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)
    
    try:
        raw_proxy = os.environ.get("PROXY_URL", "")
        proxy_config = parse_proxy(raw_proxy)

        start_time = time.time()
        token = solve_turnstile(proxy_config, headless=True)
        elapsed = time.time() - start_time

        logger.removeHandler(ch)
        log_contents = log_capture_string.getvalue()

        if token:
            return JSONResponse(content={
                "success": True,
                "token": token,
                "elapsed_s": round(elapsed, 2),
                "logs": log_contents
            })
        else:
            return JSONResponse(
                content={"success": False, "error": "Failed to solve Turnstile", "logs": log_contents},
                status_code=503
            )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Unhandled exception in getToken: {tb}")
        logger.removeHandler(ch)
        return JSONResponse(
            content={"success": False, "error": str(e), "traceback": tb, "logs": log_capture_string.getvalue()},
            status_code=500
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
