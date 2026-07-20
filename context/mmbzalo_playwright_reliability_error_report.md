# MMBZalo Playwright Reliability Incident Report & Fix Plan

## 1. Summary

The application’s core flows work in isolated tests, but Playwright browser ownership is not reliable across repeated actions, multiple workspaces, and both runtime processes.

Confirmed failures:

```text
API login flow:
POST /api/login/start
→ 500 Internal Server Error
→ RuntimeError:
  Sync Playwright initialization escaped its dedicated owner thread.

Worker contact-sync flow:
POST /api/contacts/sync
→ 200 job accepted
→ background job fails
→ Contact sync error:
  Sync Playwright initialization escaped its dedicated owner thread.
```

Previous related failure:

```text
Contact sync could fail with Chromium SingletonLock
because the login browser still owned the same workspace profile.
```

This is a backend lifecycle/thread-affinity problem, not primarily a frontend problem and not caused by the Zalo account used for QR scanning.

---

## 2. Confirmed Reproduction Paths

### Path A — API login failure

```text
1. Run previous login/browser operations.
2. Create or select another workspace.
3. Click Connect Zalo.
4. UI calls POST /api/login/start.
5. API returns HTTP 500.
```

Confirmed call chain:

```text
app/main.py: login_start
→ await driver.start_login()
→ _run_in_thread(self._start_login_sync)
→ _playwright_thread.submit(...)
→ _start_login_sync()
→ self._ensure_pw()
→ RuntimeError:
  Sync Playwright initialization escaped its dedicated owner thread.
```

### Path B — worker contact-sync failure

```text
1. Create a new MMBZalo account and workspace.
2. Connect Zalo successfully through QR.
3. Open Contacts.
4. Click Sync Contacts.
5. API returns 200 because the job is queued.
6. Worker later fails with the same owner-thread RuntimeError.
```

### Path C — profile-lock failure observed earlier

```text
1. QR login browser remains open after authentication.
2. Contact sync starts with the same persistent profile.
3. Chromium rejects the second process because SingletonLock exists.
```

---

## 3. Important Distinctions

### Different Zalo accounts do not solve the thread bug

Expected isolation:

```text
Workspace A → Zalo account A → profile A
Workspace B → Zalo account B → profile B
```

This prevents Zalo-session overlap but does not fix Playwright thread ownership.

The owner-thread RuntimeError happens before Zalo account identity matters.

### Same Zalo account in multiple workspaces is a separate risk

```text
Workspace A → Zalo account X
Workspace B → Zalo account X
```

Possible consequences:

```text
Session invalidation
Unexpected logout
Conflicting saved browser state
Unclear ownership of the Zalo identity
```

Treat this separately from the Playwright lifecycle defect.

---

## 4. Root-Cause Hypotheses to Verify

Investigate all of the following:

```text
1. Playwright is initialized on one thread and reused on another.
2. A global owner thread is mixed with per-driver Playwright state.
3. The owner thread is recreated while stale self._pw state remains.
4. Some Playwright calls bypass the owner-thread submission path.
5. Driver objects survive after their owner thread shuts down.
6. Thread IDs are cached globally or inconsistently.
7. API and worker use similar but independently broken lifecycle code.
8. Repeated requests race during initialization or shutdown.
9. Browser/context/page references remain after close or failure.
10. Shutdown does not clear all runtime ownership state.
```

Search all assignments and uses of:

```text
self._pw
self._browser
self._context
self._page
owner_thread_id / playwright_thread_id
_playwright_thread
sync_playwright().start()
playwright.stop()
_run_in_thread
asyncio.to_thread
run_in_executor
```

---

## 5. Required Architecture

Choose one architecture and apply it consistently.

### Option A — persistent owner runtime per process

Recommended compact fix:

```text
API process
→ one persistent Playwright owner thread
→ all QR login browser operations submitted to it

Worker process
→ one persistent Playwright owner thread
→ all sync/send/campaign operations submitted to it
```

Rules:

```text
Playwright must be created on the owner thread.
Every browser/context/page call must execute on that same thread.
Shutdown must also execute on that same thread.
No direct Playwright calls from FastAPI or worker asyncio threads.
```

### Option B — dedicated single-thread executor per driver

Acceptable when each workspace driver owns its browser runtime:

```python
ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix=f"zalo-{workspace_id}",
)
```

All operations for that driver must use the same executor for its full lifetime.

### Option C — Async Playwright refactor

Long-term preferred design:

```text
playwright.async_api
→ remove Sync Playwright thread-affinity complexity
```

This is a larger change and should not be mixed partially with the current sync implementation.

---

## 6. Lifecycle Contract

For each Playwright runtime:

```text
CREATE
owner thread starts
→ sync_playwright().start()
→ record owner thread ID
→ runtime becomes ready

USE
launch persistent context
→ page operations
→ screenshots
→ authentication checks
→ contact sync / send operations

CLOSE
close page/context/browser on owner thread
→ playwright.stop() on owner thread
→ clear all references
→ clear owner thread ID
→ mark runtime stopped
```

Never retain:

```text
self._pw
self._browser
self._context
self._page
owner thread ID
```

after the owner runtime has stopped or failed.

Initialization and shutdown must be idempotent.

---

## 7. Workspace Browser Ownership

Enforce one active browser owner per workspace profile.

```text
workspace profile
→ either login browser
→ or worker browser
→ never both simultaneously
```

After QR authentication:

```text
authenticated detected
→ close login browser
→ wait for Chromium profile lock release
→ mark profile available
→ enable contact sync
```

Before worker launch:

```text
verify no live browser owns the profile
verify no SingletonLock remains from a live process
acquire application-level workspace lock
launch browser
release lock after complete shutdown
```

Do not blindly delete Chromium lock files while a browser process is alive.

---

## 8. Concurrency and Deduplication

### Backend

Allow only one active browser job per workspace.

Reject or reuse duplicate jobs when one is already:

```text
queued
running
stopping
```

Apply to:

```text
contact_sync
manual_send
campaign execution
QR login start
```

Suggested response:

```text
409 Conflict
job_already_running
existing_job_id
```

### Frontend

Immediately disable action buttons after the first click:

```text
Connect Zalo → Connecting...
Sync Contacts → Syncing...
Save + Execute → Starting...
```

Do not re-enable until success or handled failure.

Observed repeated calls must be prevented:

```text
POST /api/login/start repeated within seconds
POST /api/contacts/sync retried while earlier jobs were unresolved
```

---

## 9. Error Handling

Do not return raw HTTP 500 for expected browser-state failures.

Map internal failures to structured responses:

```json
{
  "error_code": "PLAYWRIGHT_RUNTIME_UNAVAILABLE",
  "message": "Browser service is temporarily unavailable.",
  "retryable": true
}
```

Useful error codes:

```text
PLAYWRIGHT_RUNTIME_UNAVAILABLE
PLAYWRIGHT_THREAD_MISMATCH
WORKSPACE_BROWSER_BUSY
PROFILE_LOCKED
ZALO_NOT_AUTHENTICATED
QR_EXPIRED
JOB_ALREADY_RUNNING
BROWSER_START_FAILED
```

UI should show the real job outcome, not only:

```text
Check the Zalo connection, then try again.
```

Keep technical traceback in server logs and optional Technical details UI.

---

## 10. Same-Zalo-Account Policy

For production, prefer:

```text
one workspace
→ one Zalo identity
→ one persistent browser profile
```

Add one of these:

```text
A. Block the same detected Zalo identity from linking to multiple workspaces.
B. Allow it but show a clear warning about session invalidation.
C. Provide an explicit transfer/relink flow.
```

Do not rely on profile isolation alone to guarantee Zalo allows concurrent sessions.

---

## 11. Observability

Add structured logs for every browser operation:

```text
process_role=api|worker
workspace_id
driver_id
operation
thread_name
thread_id
owner_thread_id
runtime_state
profile_path
browser_pid
job_id
duration_ms
result
error_code
```

Required events:

```text
playwright_owner_started
playwright_initialized
driver_created
browser_launch_started
browser_launched
qr_capture_succeeded
authentication_detected
browser_close_started
browser_closed
profile_released
job_started
job_succeeded
job_failed
playwright_stopped
```

On thread mismatch, log both IDs before raising.

---

## 12. Regression Test Matrix

Run without restarting API or worker between steps.

### Single workspace

```text
1. Connect Zalo.
2. Stop login.
3. Connect again.
4. Authenticate.
5. Sync contacts.
6. Sync contacts again.
7. Send one manual message.
8. Run one small campaign.
```

### Multiple workspaces, different Zalo accounts

```text
1. Workspace A connects Zalo A.
2. Workspace A syncs contacts.
3. Workspace A sends a message.
4. Workspace B connects Zalo B.
5. Workspace B syncs contacts.
6. Workspace B sends a message.
7. Workspace A performs another sync/send.
8. Repeat across both workspaces.
```

### Concurrent actions

```text
1. Double-click Connect Zalo.
2. Double-click Sync Contacts.
3. Start sync while campaign is running.
4. Start campaign while another browser job owns the workspace.
```

Expected:

```text
No duplicate browser launch
No duplicate queued job
No thread mismatch
No SingletonLock
Clear 409/busy response where appropriate
```

### Failure recovery

```text
1. Force browser launch failure.
2. Verify runtime state is cleared.
3. Retry without restarting service.
4. Stop during QR wait.
5. Reconnect.
6. Restart API/worker during idle and during a queued job.
```

---

## 13. Acceptance Criteria

The fix is complete only when:

```text
Connect Zalo never returns the owner-thread RuntimeError.
Contact sync never returns the owner-thread RuntimeError.
API and worker can serve multiple workspaces sequentially.
Different Zalo accounts work in isolated workspace profiles.
Login browser releases the profile before worker jobs begin.
No SingletonLock appears during normal flow.
Duplicate UI requests do not create duplicate jobs.
Failures return structured, user-readable errors.
A failed runtime can recover without service restart.
Regression matrix passes with the same long-running processes.
```

---

## 14. Implementation Order

```text
1. Instrument current owner-thread and lifecycle state.
2. Reproduce both API and worker failures in tests.
3. Choose one Playwright ownership architecture.
4. Fix API QR login runtime.
5. Fix worker sync/send runtime.
6. Add workspace-level browser locking.
7. Add job deduplication and frontend button guards.
8. Add structured errors.
9. Add same-Zalo-account policy.
10. Run full regression matrix.
```

---

## 15. Temporary Operational Workaround

Until fixed:

```text
Restart mmbzalo-api after API login runtime becomes corrupted.
Restart mmbzalo-worker after worker runtime becomes corrupted.
Use one workspace/browser action at a time.
Use one Zalo account per workspace.
Do not click Connect or Sync repeatedly.
```

These are temporary recovery steps, not an acceptable production solution.
