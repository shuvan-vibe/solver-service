# Solver-Service

Self-hosted Cloudflare Turnstile solver for the Socpublic scanners. It replaces
paid providers such as Azcaptcha with a SeleniumBase UC browser running through
the same Xray/VLESS VPN as the proxy scanner.

The expensive parts are **on demand**:

- Xray starts with the first solve request and stops after `XRAY_IDLE_TIMEOUT`
  seconds without use.
- The browser starts with the first solve request and stops after
  `BROWSER_IDLE_TIMEOUT` seconds without use.
- The FastAPI process stays alive so the Cloudflare tunnel keeps a URL.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /` | no | Liveness |
| `GET /health` | `X-API-Key` | Xray/browser status |
| `GET /getSocpublicToken` | `X-API-Key` | Token for `socpublic.com` login |
| `GET /getToken` | `X-API-Key` | Legacy foxigrow token |
| `GET /solve?sitekey=&pageurl=` | `X-API-Key` | Generic token |
| `GET /warmup` | `X-API-Key` | Pre-start Xray + browser |

Successful solve:

```json
{
  "success": true,
  "token": "0.xxxxx...",
  "elapsed_s": 18.4,
  "attempts": 1
}
```

Failure / busy:

```json
{ "success": false, "error": "Failed after all retries", "elapsed_s": 120.0 }
```

`429` means another solve is already running. `401` means a bad API key.
`503` means the tunnel or solve failed.

## Local setup

```bash
cd Solver-Service
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
```

Fill in `.env`:

```env
SOLVER_API_KEY=local-dev-key-12345
SOLVER_NETWORK_MODE=xray
XRAY_VLESS_CONFIG=vless://...
```

Run the Xray-only test first:

```bash
python test_xray.py
```

Then run a real solve without starting the web server:

```bash
python local_test.py
```

Or start the API:

```bash
python main.py
```

and test it:

```bash
python test_api.py --url http://127.0.0.1:8080 --key local-dev-key-12345 --solve
```

> On Windows, SeleniumBase UC mode opens a real Chromium window. Run it from a
> desktop session, not a headless service. On Linux the service starts Xvfb
> automatically (`xvfb=True`).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SOLVER_API_KEY` | empty | Shared secret. Empty disables auth (local only). |
| `SOLVER_NETWORK_MODE` | auto | `xray`, `proxy`, or `direct`. Auto picks Xray if a VLESS config exists, otherwise `PROXY_URL`, otherwise direct. |
| `XRAY_VLESS_CONFIG` | empty | Full `vless://` URL. Same VPN as the proxy scanner. |
| `XRAY_SUBSCRIPTION_URL` | empty | Alternative to a full VLESS URL. |
| `XRAY_PREFERRED_SERVER` | empty | Substring of the server name to pick from a subscription. |
| `PROXY_URL` | empty | Optional residential proxy fallback. |
| `XRAY_IDLE_TIMEOUT` | `600` | Seconds before an idle Xray process is stopped. |
| `BROWSER_IDLE_TIMEOUT` | `900` | Seconds before an idle browser is closed. |
| `BROWSER_MAX_SOLVES` | `250` | Restart the browser after this many solves. |
| `SOLVER_MAX_RETRIES` | `2` | Solve attempts per API request. |
| `SOLVER_MAX_WAIT` | `180` | Max seconds to wait for the solve lock. |
| `SOLVER_HEADLESS` | `false` | Keep `false` for UC mode; headless reduces success. |
| `MONGO_URI` | empty | Enables the URL heartbeat for GitHub Actions discovery. |
| `SOLVER_PUBLIC_URL` | empty | Public tunnel URL written to MongoDB. |
| `XRAY_VERSION` | `1.8.7` | Xray-core release version. |
| `XRAY_DIR` | `/tmp/xray` | Where the binary/config/log live. |
| `XRAY_BINARY` | auto | Explicit Xray binary path. |
| `XRAY_PROXY_SCHEME` | `http` | `http` uses port 10809, `socks5` uses 10808. |
| `PORT` | `8080` | Uvicorn port. |

## GitHub Actions + Cloudflare quick tunnel

The workflow at `.github/workflows/solver-service.yml` (repo root) or
`Solver-Service/.github/workflows/solver.yml` (standalone repo) does this:

1. Installs Linux deps + Python requirements.
2. Caches the Xray binary and SeleniumBase browser.
3. Starts a `cloudflared` quick tunnel.
4. Extracts the `https://*.trycloudflare.com` URL.
5. Starts the solver with `SOLVER_PUBLIC_URL` set.
6. Writes/heartbeats the URL to MongoDB `solver_status`.
7. Health-checks every 60 seconds and keeps the job alive.
8. Restarts every 5 hours (GitHub caps jobs at 6 hours).

### Required GitHub secrets

| Secret | Required | Purpose |
|---|---|---|
| `SOLVER_API_KEY` | yes | Shared with the scanners. |
| `XRAY_VLESS_CONFIG` | yes | The same VLESS URL the proxy scanner uses. |
| `MONGO_URI` | yes for quick tunnel | The bot reads the current tunnel URL from here. |
| `PROXY_URL` | no | Fallback residential proxy. |

### URL discovery (no domain needed)

You do **not** need a domain. The quick tunnel URL changes on every run, so the
service writes it to MongoDB:

```
socpublic_db.solver_status
{
  "_id": "active",
  "url": "https://random-name.trycloudflare.com",
  "healthy": true,
  "network_mode": "xray",
  "updated_at": ISODate(...)
}
```

The scanners will read this document when `SOLVER_SERVICE_URL` is empty. A stale
`updated_at` (default 15 minutes) means "do not use".

If you later get a domain, switch to a named Cloudflare tunnel and set
`SOLVER_SERVICE_URL` directly; the DB discovery can stay as a fallback.

## How the scanners will use it

This is the next phase; the scanner code is intentionally untouched for now.

```env
SOLVER_SERVICE_ENABLED=1
SOLVER_SERVICE_API_KEY=<same key>
# SOLVER_SERVICE_URL=            # leave blank for DB discovery
SOLVER_SERVICE_TIMEOUT=240
```

Flow:

```
cookie expired
  -> GET {solver_url}/getSocpublicToken  (X-API-Key)
  -> token
  -> POST socpublic login with cf-turnstile-response=<token>
  -> save cookie
  -> on failure, fall back to AZCAPTCHA_KEYS
```

## Security notes

- Always set `SOLVER_API_KEY` in production.
- Do not commit `.env`.
- The quick tunnel URL is public; the API key is the only gate.
- The service serializes solves, so one job is shared safely by both scanners.
- `/health` requires the API key when one is configured.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Xray tunnel unavailable` | Bad `XRAY_VLESS_CONFIG`, blocked outbound port, or Xray binary missing. Check `XRAY_DIR/xray.log`. |
| `Turnstile API slow to load` | Page did not load through the proxy. Check exit IP with `test_xray.py`. |
| `Token rejected by Socpublic` | Turnstile IP binding or stale token. Use the token immediately. |
| `401` | `SOLVER_API_KEY` mismatch. |
| `429` | Another solve is running; the second request waited `SOLVER_MAX_WAIT` and gave up. |
| Quick tunnel URL missing from Mongo | `MONGO_URI`/`SOLVER_PUBLIC_URL` not set, or the heartbeat did not run. |
