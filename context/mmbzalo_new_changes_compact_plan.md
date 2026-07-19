# MMBZalo Compact Plan: QR Login UX + Browser Profile Lock Fix

## Goal

Make real users complete Zalo onboarding from the public domain only:

```text
User registers/logs in
→ enters own workspace
→ clicks Connect Zalo
→ scans QR inside web UI
→ backend authenticates Zalo session
→ login browser auto-closes
→ user syncs contacts
```

No noVNC should be needed for normal users.

## Current State

Working:

```text
User registration API
Default workspace creation
Per-workspace browser profile path
/api/login/start returns waiting_qr
Zalo QR scan can authenticate through noVNC
Workspace session becomes authenticated
```

Problems:

```text
QR is visible only in VPS/noVNC, not in public frontend
Login browser remains open after QR scan
Contact sync can fail with SingletonLock because the same Chrome profile is still open
```

## Change 1: Show QR in Public Frontend

### Backend

Update login flow so `/api/login/status` can return QR image data.

Possible response shape:

```json
{
  "state": "waiting_qr",
  "message": "Waiting for QR scan...",
  "qr_image_base64": "data:image/png;base64,...",
  "timestamp": "..."
}
```

Implementation options:

```text
Option A: screenshot QR area from Playwright page
Option B: extract QR image src if Zalo exposes it in DOM
Option C: expose temporary screenshot URL from backend
```

Preferred MVP:

```text
Capture QR area screenshot
Return base64 PNG in /api/login/status
Frontend renders it directly
```

### Frontend

Add Connect Zalo onboarding UI:

```text
Click Connect Zalo
→ POST /api/login/start
→ poll GET /api/login/status every 1-2 seconds
→ display QR image when state = waiting_qr
→ show connected state when state = authenticated
```

Frontend states:

```text
idle: show Connect Zalo button
waiting_qr: show QR image + instructions
authenticated: show Connected + Sync Contacts button
error: show retry/stop login button
```

## Change 2: Auto-Close Login Browser After QR Scan

### Current issue

After QR scan succeeds:

```text
workspace_session becomes authenticated
but login Chrome remains open
profile lock remains active
contact sync worker cannot open same profile
```

### Required behavior

```text
QR scan succeeds
→ update workspace_session to authenticated
→ close Playwright page/context/browser
→ release Chrome profile lock
→ allow contact sync
```

### Backend implementation

In login status watcher or authentication detection code:

```text
if authenticated:
    update workspace_session
    close login browser/context
    clear in-memory login driver/session
```

Make this idempotent:

```text
Calling stop after browser is already closed should not fail
```

## Change 3: Guard Contact Sync Against Active Login Browser

Before queuing or running contact sync:

```text
check whether login browser is active for this workspace
```

MVP behavior:

```text
if login browser is active:
    close it automatically
    wait until profile lock is released
    queue contact_sync
```

Alternative safer behavior:

```text
return HTTP 409:
"Login browser is still open. Please finish or stop Zalo login before syncing contacts."
```

Preferred product behavior:

```text
auto-close login browser after successful authentication
```

## Change 4: Handle Profile Lock Gracefully

When worker sees Chrome profile lock:

```text
do not mark as permanent failure immediately
```

Better behavior:

```text
detect SingletonLock
wait/retry for short time
if lock disappears: continue sync
if lock remains: fail with clear message
```

Suggested retry logic:

```text
retry every 2 seconds for up to 20 seconds
if lock disappears: continue sync
if lock remains: fail with clear message
```

Clear error message:

```text
Workspace browser profile is currently in use. Close login browser and retry contact sync.
```

## Change 5: One Browser Owner Per Workspace

Enforce this backend rule:

```text
Only one browser process may own a workspace profile at a time.
```

Possible owner types:

```text
login_browser
contact_sync_worker
manual_send_worker
campaign_worker
```

Before launching Chrome:

```text
check active owner
if same workspace has active owner, block or wait
```

## Recommended Implementation Order

```text
1. Add QR screenshot/base64 to /api/login/status.
2. Add frontend QR rendering and polling.
3. Auto-close login browser after authenticated state.
4. Add contact sync guard to close/block active login browser.
5. Add graceful SingletonLock handling in worker.
6. Retest new-user flow end-to-end.
```

## Test Plan

### Test 1: New user signup

```text
Register new user
Confirm default workspace created
Confirm workspace_session is idle
```

### Test 2: QR visible without noVNC

```text
Log in on https://linhanhchip.site
Click Connect Zalo
Confirm QR appears in frontend
Do not open noVNC
Scan QR from frontend
```

Expected:

```text
/api/login/status → authenticated
/api/workspace-session → last_authenticated_at not null
```

### Test 3: Browser auto-closes

After QR scan:

```bash
pgrep -af "/var/lib/app/browser-profiles/<workspace-id>" || echo "Profile is free"
```

Expected:

```text
No Chrome process using this profile
```

### Test 4: Contact sync succeeds

```text
Click Sync Contacts
Poll job status
```

Expected:

```text
status = succeeded
contacts appear in workspace
```

### Test 5: Workspace isolation

```text
User A cannot see User B contacts
User A cannot use User B workspace/session/profile
```

## Acceptance Criteria

```text
Real user can connect Zalo without noVNC
QR appears on public domain UI
QR scan changes workspace_session to authenticated
Login browser closes automatically after authentication
Contact sync no longer fails with SingletonLock after QR scan
Each workspace keeps its own Zalo profile path
```

## MVP Decision

Implement first:

```text
QR screenshot in /api/login/status
Frontend QR display
Auto-close login browser after authenticated
```

Then improve:

```text
workspace browser lock manager
worker retry on SingletonLock
member/invite/role management
```
