# MMBZalo Error Report: `/api/login/start` HTTP 500

## Summary

The new registration flow is working, and a new user/workspace can be created successfully. However, when the new user tries to start the Zalo QR login flow, the endpoint below returns `500 Internal Server Error`:

```text
POST /api/login/start
```

The backend reaches the correct workspace and browser profile path, but then crashes because Playwright Sync API is being started inside an async FastAPI request path.

## Environment

```text
Domain: https://linhanhchip.site
Backend: FastAPI / Uvicorn
Process manager: systemd
Service: mmbzalo-api
Browser runtime: Playwright + Chrome + Xvfb
Display: :99
Host identity: inet-vps-01
```

## Test User / Workspace

The test user registration succeeded.

```text
Email: testuser1@example.com
User ID: 40fb351d-748e-4ada-bd1c-7261fc7926c0
Workspace ID: 383fa718-e682-442c-b1e2-7b2601775ad2
Workspace slug: test-user-workspace
Workspace name: Test User Workspace
Role: admin
Login state before QR: idle
```

Workspace session response:

```json
{
  "workspace_id": "383fa718-e682-442c-b1e2-7b2601775ad2",
  "owner_worker_id": "inet-vps-01",
  "login_state": "idle",
  "profile_path": "/var/lib/app/browser-profiles/383fa718-e682-442c-b1e2-7b2601775ad2",
  "profile_name": null,
  "profile_avatar_url": null,
  "phone_number": null,
  "last_authenticated_at": null,
  "last_validated_at": null,
  "error_message": null
}
```

## Reproduction Steps

1. Register a new test user.
2. Log in as the test user.
3. Confirm the new workspace exists.
4. Call `/api/login/start` with the test user's authenticated cookie.

Command used:

```bash
curl -i -b /tmp/testuser.cookie -c /tmp/testuser.cookie \
  -X POST https://linhanhchip.site/api/login/start \
  -H "Content-Type: application/json" \
  --data '{}'
```

Observed response:

```text
HTTP/2 500
content-type: text/plain; charset=utf-8

Internal Server Error
```

Then checking login status:

```bash
curl -fsS -b /tmp/testuser.cookie \
  https://linhanhchip.site/api/login/status | python -m json.tool
```

Observed response:

```json
{
  "state": "idle",
  "profile_name": null,
  "profile_avatar": null,
  "phone_number": null,
  "message": "No login browser is open.",
  "timestamp": "2026-07-18T14:11:47.853920"
}
```

## Relevant API Log

Command used:

```bash
sudo journalctl -u mmbzalo-api --since "10 minutes ago" --no-pager -l
```

Important log lines:

```text
Initialized ZaloDriver workspace=383fa718-e682-442c-b1e2-7b2601775ad2 host_identity=inet-vps-01 display=:99 profile_path=/var/lib/app/browser-profiles/383fa718-e682-442c-b1e2-7b2601775ad2

POST /api/login/start HTTP/1.1" 500 Internal Server Error
```

Traceback path:

```text
File "/opt/app/current/app/main.py", line 331, in login_start
    result = LoginStatus(**(await driver.start_login()))

File "/opt/app/current/app/zalo_driver.py", line 314, in start_login
    return await _run_in_thread(self._start_login_sync)

File "/opt/app/current/app/zalo_driver.py", line 276, in _start_login_sync
    self._ensure_pw()

File "/opt/app/current/app/zalo_driver.py", line 184, in _ensure_pw
    self._pw = sync_playwright().start()
```

Final error:

```text
playwright._impl._errors.Error: It looks like you are using Playwright Sync API inside the asyncio loop.
Please use the Async API instead.
```

## Diagnosis

This is not a DNS, Caddy, HTTPS, cookie, registration, workspace creation, or VPS permission issue.

Confirmed working:

```text
Registration API works
New user is created
New workspace is created
Workspace session row is created
Correct browser profile path is generated
API is reachable through HTTPS domain
```

The failure happens when `/api/login/start` initializes Playwright.

The code path uses:

```python
sync_playwright().start()
```

inside a request handled by FastAPI's async runtime. Playwright detects that the Sync API is being used inside an asyncio event loop and raises an exception.

## Root Cause

The Zalo login flow uses Playwright Sync API in an async FastAPI endpoint path.

Current problematic path:

```text
FastAPI async route
→ await driver.start_login()
→ _run_in_thread(self._start_login_sync)
→ self._ensure_pw()
→ sync_playwright().start()
→ Playwright error
```

Even though part of the logic is wrapped in a thread helper, Playwright is still detecting the sync API in an asyncio loop context.

## Recommended Fix

Refactor the Zalo login flow to use Playwright Async API.

Replace:

```python
from playwright.sync_api import sync_playwright
```

with:

```python
from playwright.async_api import async_playwright
```

Then change Playwright initialization from:

```python
self._pw = sync_playwright().start()
```

to:

```python
self._pw = await async_playwright().start()
```

Browser and page operations should also become async:

```python
context = await self._pw.chromium.launch_persistent_context(...)
page = context.pages[0] if context.pages else await context.new_page()
await page.goto(...)
await page.screenshot(...)
await context.close()
```

## Alternative Fix

If a full async refactor is not desired immediately, move the whole Zalo login browser flow out of the FastAPI request path.

Possible architecture:

```text
POST /api/login/start
→ enqueue login-start job
→ worker owns Playwright browser lifecycle
→ API only returns login status from shared state/database
```

This avoids running Playwright Sync API inside the FastAPI async request context.

However, the preferred long-term fix is:

```text
FastAPI async route
→ async ZaloDriver
→ async Playwright
```

## Expected Behavior After Fix

After the fix, this command should return `200` instead of `500`:

```bash
curl -i -b /tmp/testuser.cookie -c /tmp/testuser.cookie \
  -X POST https://linhanhchip.site/api/login/start \
  -H "Content-Type: application/json" \
  --data '{}'
```

Then this command should show an active login state or QR-related status:

```bash
curl -fsS -b /tmp/testuser.cookie \
  https://linhanhchip.site/api/login/status | python -m json.tool
```

Expected general result:

```text
state is not idle
login browser is active
QR/login status is available
```

## Retest Plan After Code Update

After the developer fixes the code and pushes changes:

```bash
cd /opt/app/current
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart mmbzalo-api mmbzalo-worker
sleep 5
```

Verify services:

```bash
sudo systemctl status mmbzalo-api --no-pager
sudo systemctl status mmbzalo-worker --no-pager

curl -fsS https://linhanhchip.site/api/health | python -m json.tool
curl -fsS https://linhanhchip.site/api/readiness | python -m json.tool
```

Retest login start:

```bash
curl -i -b /tmp/testuser.cookie -c /tmp/testuser.cookie \
  -X POST https://linhanhchip.site/api/login/start \
  -H "Content-Type: application/json" \
  --data '{}'

curl -fsS -b /tmp/testuser.cookie \
  https://linhanhchip.site/api/login/status | python -m json.tool
```

If login start succeeds, continue with:

```text
Scan Zalo QR
Confirm workspace_session updates
Sync contacts
Confirm contacts are isolated to the new workspace
Send one test message
```

## Status

```text
Registration API: working
Default workspace creation: working
Workspace session creation: working
Per-workspace browser profile path: working
Zalo QR login start: failing with HTTP 500
Root cause: Playwright Sync API used inside async FastAPI path
Priority: high, because new users cannot connect their Zalo account yet
```
