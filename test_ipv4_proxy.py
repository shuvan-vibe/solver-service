import time
from seleniumbase import SB

# Format: USERNAME:PASSWORD@HOST:PORT
PROXY_STRING = "itzdip07:NNzQCJjght@50.2.205.152:50100"
SITEKEY = "0x4AAAAAADuXG2nt8DMgL_NF"
PAGEURL = "https://example.com"

def test_proxy():
    print(f"Launching headed browser with proxy: {PROXY_STRING}")
    
    with SB(
        uc=True,
        proxy=PROXY_STRING,
        headless=False
    ) as sb:
        print(f"Navigating to {PAGEURL} ...")
        sb.open(PAGEURL)
        time.sleep(5)
        
        print("Injecting Turnstile widget...")
        html_content = f"""
        <div id="cf-turnstile-container" style="position: absolute; top: 0; left: 0; z-index: 9999; background: white; padding: 20px;"></div>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>
        <script>
            window.turnstileToken = null;
            turnstile.ready(function() {{
                turnstile.render('#cf-turnstile-container', {{
                    sitekey: '{SITEKEY}',
                    callback: function(token) {{
                        window.turnstileToken = token;
                        console.log("Token received:", token);
                    }},
                    'error-callback': function(err) {{
                        console.error("Turnstile error:", err);
                        window.turnstileError = err || "Unknown Error";
                    }}
                }});
            }});
        </script>
        """
        
        try:
            # Inject safely without destroying the body
            sb.driver.execute_script(
                "document.body.insertAdjacentHTML('afterbegin', arguments[0]);", html_content
            )
        except Exception as e:
            print(f"\n[!] CRASH PREVENTED: Could not inject widget. Reason: {e}")
            return

        print("Waiting for Turnstile to load. Watch the browser!")
        time.sleep(3)

        print("Attempting to auto-solve (uc_gui_click_captcha)...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"uc_gui_click_captcha error: {e}")

        print("\n--- OBSERVATION MODE ---")
        for i in range(60):
            token = sb.driver.execute_script("return window.turnstileToken;")
            error = sb.driver.execute_script("return window.turnstileError;")
            
            if token:
                print(f"SUCCESS! Token generated: {token[:40]}...")
                break
            if error:
                print(f"ERROR! Turnstile failed with error code: {error}")
                break
            
            time.sleep(1)
            
        print("\nTest finished. Keeping browser open for 30 more seconds before closing...")
        time.sleep(30)

if __name__ == "__main__":
    test_proxy()
