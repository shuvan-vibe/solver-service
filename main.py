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
    window.__turnstileWidgetId = null;
    window.__turnstileRenderError = null;

    var container = document.createElement('div');
    container.id = '__cf_turnstile_solver';
    container.style.cssText = 'position:fixed;top:0;left:0;z-index:2147483647;background:white;padding:5px;';
    document.body.appendChild(container);

    var script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__onTurnstileLoad';
    script.async = true;
    script.defer = true;
    script.onerror = function(e) {{
        window.__turnstileRenderError = 'Script failed to load: ' + String(e);
        console.log('[Solver] Script load error: ' + e);
    }};

    window.__onTurnstileLoad = function() {{
        console.log('[Solver] Turnstile API loaded, calling render...');
        try {{
            var widgetId = window.turnstile.render('#__cf_turnstile_solver', {{
                sitekey: '{SITEKEY}',
                appearance: 'always',
                theme: 'light',
                callback: function(token) {{
                    window.__turnstileToken = token;
                    console.log('[Solver] Token received: ' + token.slice(0, 20) + '...');
                }},
                'error-callback': function(code) {{
                    window.__turnstileError = String(code);
                    console.log('[Solver] Error: ' + code);
                }},
                'expired-callback': function() {{
                    window.__turnstileToken = null;
                    console.log('[Solver] Token expired');
                }}
            }});
            window.__turnstileWidgetId = widgetId;
            console.log('[Solver] Widget rendered, id=' + widgetId);
            console.log('[Solver] Container innerHTML length: ' + container.innerHTML.length);
        }} catch(e) {{
            window.__turnstileRenderError = String(e);
            console.log('[Solver] Render error: ' + e);
        }}
    }};

    document.head.appendChild(script);
    console.log('[Solver] Turnstile widget injected');
}})();
"""


def solve_turnstile(proxy_str: Optional[str] = None) -> Optional[str]:
    """Navigate to the target page, inject Turnstile, let UC browser auto-solve, return token."""
    from seleniumbase import SB
    import platform

    is_linux = platform.system() == "Linux"
    logger.info(f"Launching SeleniumBase UC browser (Linux={is_linux})...")
    try:
        # On Linux (Railway), use xvfb=True for virtual display (NOT headless!)
        # On Windows (local), use headed mode normally
        with SB(uc=True, proxy=proxy_str, xvfb=is_linux) as sb:
            logger.info(f"Navigating to {PAGEURL}...")
            sb.open(PAGEURL)

            # Wait for the page body to exist so we can append the Turnstile container
            sb.wait_for_element("body", timeout=30)

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

            # Wait for the Turnstile API script to load (it loads async)
            logger.info("Waiting for Turnstile API to load...")
            api_loaded = False
            for attempt in range(30):  # Up to 15 seconds
                time.sleep(0.5)
                try:
                    status = sb.execute_script("""(function(){
                        var container = document.getElementById('__cf_turnstile_solver');
                        return {
                            turnstileExists: typeof window.turnstile !== 'undefined',
                            injected: !!window.__turnstileInjected,
                            token: window.__turnstileToken || null,
                            error: window.__turnstileError || null,
                            renderError: window.__turnstileRenderError || null,
                            widgetId: window.__turnstileWidgetId,
                            scriptTags: document.querySelectorAll('script[src*="challenges.cloudflare"]').length,
                            iframeCount: document.querySelectorAll('iframe').length,
                            containerExists: !!container,
                            containerHTML: container ? container.innerHTML.substring(0, 200) : 'NO CONTAINER'
                        };
                    })()""")
                    logger.info(f"Check {attempt}: {status}")
                    if status and status.get('turnstileExists'):
                        api_loaded = True
                        logger.info("Turnstile API loaded!")
                        time.sleep(3)  # Extra time for iframe to render
                        break
                    if status and status.get('token'):
                        logger.info(f"Token already available! {status['token'][:20]}...")
                        return status['token']
                except Exception as e:
                    logger.error(f"Check {attempt} failed: {e}")

            if not api_loaded:
                logger.error("Turnstile API script never loaded! The CDN might be blocked.")

            # Discover all iframes
            iframe_info = sb.execute_script("""(function(){
                var iframes = document.querySelectorAll('iframe');
                var results = [];
                for (var i = 0; i < iframes.length; i++) {
                    var rect = iframes[i].getBoundingClientRect();
                    results.push({
                        src: iframes[i].src || '',
                        x: rect.x, y: rect.y,
                        width: rect.width, height: rect.height,
                        id: iframes[i].id || '',
                        name: iframes[i].name || ''
                    });
                }
                return results;
            })()""")
            logger.info(f"Found {len(iframe_info) if iframe_info else 0} iframes: {iframe_info}")

            # Find the Turnstile iframe (look for challenges.cloudflare or cf-turnstile)
            turnstile_iframe = None
            if iframe_info:
                for iframe in iframe_info:
                    src = iframe.get('src', '')
                    if 'challenges.cloudflare' in src or 'turnstile' in src or 'cf-chl' in src:
                        turnstile_iframe = iframe
                        break
                # If not found by src, pick the first iframe in our container
                if not turnstile_iframe and iframe_info:
                    for iframe in iframe_info:
                        if iframe.get('width', 0) > 50 and iframe.get('height', 0) > 30:
                            turnstile_iframe = iframe
                            break

            if turnstile_iframe:
                logger.info(f"Turnstile iframe found: {turnstile_iframe}")
                # The checkbox is typically at ~28px from left edge, vertically centered
                click_x = int(turnstile_iframe['x']) + 28
                click_y = int(turnstile_iframe['y']) + int(turnstile_iframe['height']) // 2
                logger.info(f"Clicking at coordinates ({click_x}, {click_y})...")

                # Use CDP Input.dispatchMouseEvent for a real browser click
                try:
                    sb.execute_cdp_cmd("Input.dispatchMouseEvent", {
                        "type": "mouseMoved", "x": click_x, "y": click_y
                    })
                    time.sleep(0.1)
                    sb.execute_cdp_cmd("Input.dispatchMouseEvent", {
                        "type": "mousePressed", "x": click_x, "y": click_y,
                        "button": "left", "clickCount": 1
                    })
                    time.sleep(0.05)
                    sb.execute_cdp_cmd("Input.dispatchMouseEvent", {
                        "type": "mouseReleased", "x": click_x, "y": click_y,
                        "button": "left", "clickCount": 1
                    })
                    logger.info("CDP click dispatched successfully.")
                except Exception as e:
                    logger.error(f"CDP click failed: {e}")
                    # Fallback: try uc_gui_click_captcha
                    try:
                        sb.uc_gui_click_captcha()
                        logger.info("Fallback GUI click used.")
                    except Exception as e2:
                        logger.error(f"Fallback GUI click also failed: {e2}")
            else:
                logger.warning("No Turnstile iframe found! Trying GUI click as fallback...")
                try:
                    sb.uc_gui_click_captcha()
                    logger.info("Fallback GUI click used.")
                except Exception as e:
                    logger.error(f"GUI click failed: {e}")

            try:
                sb.save_screenshot(os.path.join(SCREENSHOT_DIR, "screenshot_after_click.png"))
                logger.info("Saved after-click screenshot.")
            except Exception:
                pass

            # Poll for the token (auto-solved by stealth browser)
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


from fastapi.staticfiles import StaticFiles

@app.get("/")
def root():
    return {"status": "ok", "service": "Turnstile Solver API"}

# Create screenshot dir if not exists
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
app.mount("/debug", StaticFiles(directory=SCREENSHOT_DIR), name="static")


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
        
        if proxy_config:
            logger.info("Using configured PROXY_URL")
        else:
            logger.warning("NO PROXY_URL CONFIGURED! Using datacenter IP (High risk of CAPTCHA block)")

        start_time = time.time()
        token = solve_turnstile(proxy_config)
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
