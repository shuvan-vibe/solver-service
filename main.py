"""
Turnstile solver microservice using FastAPI, SeleniumBase UC, and Playwright CDP.
"""

import os
import sys
import time
import logging
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from seleniumbase import sb_cdp
from playwright.sync_api import sync_playwright

app = FastAPI(title="Turnstile Solver API")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Target details
SITEKEY = "0x4AAAAAADuXG2nt8DMgL_NF"
PAGEURL = "https://tma.foxigrow.com"

# The proxy format from the provider is: HOST:PORT:USER:PASS
# Playwright needs: http://user:pass@host:port
def parse_proxy(raw_proxy: str) -> Optional[dict]:
    if not raw_proxy:
        return None
    try:
        parts = raw_proxy.strip().split(':')
        if len(parts) == 4:
            host, port, user, password = parts
            return {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": password
            }
        else:
            logger.warning(f"Unsupported proxy format (expected HOST:PORT:USER:PASS). Found {len(parts)} parts.")
            return None
    except Exception as e:
        logger.error(f"Failed to parse proxy: {e}")
        return None

def solve_turnstile(proxy_config: Optional[dict] = None) -> Optional[str]:
    logger.info("Launching SeleniumBase UC browser...")
    sb = None
    try:
        sb = sb_cdp.Chrome(guest=True, binary_location="/usr/bin/chromium")
        endpoint_url = sb.get_endpoint_url()
        logger.info(f"CDP endpoint: {endpoint_url}")

        with sync_playwright() as pw:
            # Connect playwright to the SeleniumBase stealth browser
            browser = pw.chromium.connect_over_cdp(endpoint_url)
            
            # Create a new context. If we have a proxy, apply it here.
            # Unfortunately connect_over_cdp does not let you easily inject proxy dynamically
            # to the *existing* default context of that browser instance, 
            # but we can try creating a new context with the proxy attached.
            context_args = {}
            if proxy_config:
                logger.info(f"Applying proxy: {proxy_config['server']}")
                context_args["proxy"] = {
                    "server": proxy_config["server"],
                    "username": proxy_config["username"],
                    "password": proxy_config["password"]
                }
            
            context = browser.new_context(**context_args)
            page = context.new_page()

            logger.info(f"Navigating to {PAGEURL}...")
            page.goto(PAGEURL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            logger.info("Injecting Turnstile widget...")
            page.evaluate("""(siteKey) => {
                const container = document.createElement('div');
                container.id = 'cf-turnstile-container';
                container.style.cssText = 'position:fixed;top:10px;left:10px;z-index:999999;background:#fff;padding:10px;';
                document.body.appendChild(container);

                const script = document.createElement('script');
                script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__onTurnstileReady';
                window.__onTurnstileReady = () => {
                    window.turnstile.render('#cf-turnstile-container', {
                        sitekey: siteKey,
                        callback: (token) => { window.__turnstileToken = token; },
                        'error-callback': (code) => { window.__turnstileError = String(code); }
                    });
                };
                document.head.appendChild(script);
            }""", SITEKEY)

            logger.info("Waiting for Turnstile auto-solve...")
            for i in range(60): # 30s timeout
                time.sleep(0.5)
                token = page.evaluate("() => window.__turnstileToken || null")
                if token:
                    logger.info(f"SUCCESS! Solved in {(i+1)*0.5}s")
                    return token

                error = page.evaluate("() => window.__turnstileError || null")
                if error:
                    logger.error(f"Turnstile error: {error}")
                    return None
            
            logger.error("Timeout waiting for token")
            return None

    except Exception as e:
        logger.error(f"Error during solve: {e}")
        return None
    finally:
        try:
            sb.quit()
        except:
            pass

import traceback

@app.get("/getToken")
def get_token():
    try:
        raw_proxy = os.environ.get("PROXY_URL", "")
        proxy_config = parse_proxy(raw_proxy)
        
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
            return JSONResponse(content={"error": "Failed to solve Turnstile (token was None)"}, status_code=503)
    except Exception as e:
        error_traceback = traceback.format_exc()
        logger.error(f"Unhandled exception in getToken: {error_traceback}")
        return JSONResponse(content={"error": "Internal Server Error", "traceback": error_traceback}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    # Allow overriding port via env var, default 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
