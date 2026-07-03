# Fix Zalo Driver Production Configuration Wiring

## Context

The VPS production settings are loading correctly from `.env`:

```text
host_identity: inet-vps-01
browser_profiles_root: /var/lib/app/browser-profiles
login_display: :99
```

However, the Chrome process is still being launched with the development profile directory:

```text
--user-data-dir=/opt/app/current/runtime/profiles/<workspace-id>
```

The API also reports:

```text
owner_worker_id = local-dev
profile_path = /opt/app/current/runtime/profiles/<workspace-id>
```

This confirms that the problem is no longer the `.env` file. The Zalo browser driver is likely using one of the following:

- a hard-coded development path;
- a legacy configuration field;
- a constructor default;
- a driver instance created without the current production settings.

## Files to Inspect

Focus on:

```text
app/main.py
app/zalo_driver.py
```

Search for the relevant values and driver construction:

```bash
grep -RniE 'runtime/profiles|local-dev|browser_profiles_root|host_identity|ZaloDriver' app
```

## Required Adjustment

The Zalo driver should receive production configuration from `get_settings()` rather than relying on development defaults.

Conceptually, the driver wiring should follow this pattern:

```python
settings = get_settings()

driver = ZaloDriver(
    profiles_root=settings.browser_profiles_root,
    host_identity=settings.host_identity,
    login_display=settings.login_display,
)
```

Adapt the argument names to match the existing `ZaloDriver` constructor.

Inside the driver, build the workspace-specific profile directory from the configured root:

```python
self.profile_path = Path(profiles_root) / str(workspace_id)
self.profile_path.mkdir(parents=True, exist_ok=True)
```

## Values to Replace

Replace patterns such as:

```python
Path("runtime/profiles")
Path.cwd() / "runtime" / "profiles"
owner_worker_id = "local-dev"
```

with configuration-backed values such as:

```python
settings.browser_profiles_root
settings.host_identity
settings.login_display
```

## Important Note About `owner_worker_id`

The browser profile path definitely needs correction.

Only replace:

```python
owner_worker_id = "local-dev"
```

with:

```python
settings.host_identity
```

if `owner_worker_id` is intended to identify the VPS or process that owns the workspace browser session. Inspect how the field is used before changing it.

## Validation After the Code Change

1. Stop the existing Chrome process that uses the old profile path.
2. Restart the API.
3. Restart the worker.
4. Start a new Zalo login flow.
5. Confirm the real Chrome command now contains:

```text
--user-data-dir=/var/lib/app/browser-profiles/<workspace-id>
```

6. Confirm `/api/workspace-session` reports:

```text
login_state = waiting_qr
profile_path = /var/lib/app/browser-profiles/<workspace-id>
owner_worker_id = inet-vps-01
error_message = null
```

7. Scan the new QR code.
8. Confirm the session reaches:

```text
login_state = authenticated
last_authenticated_at is populated
error_message = null
```

Only after these checks pass should testing continue to contact sync, manual send, campaign execution, and failure-path validation.
