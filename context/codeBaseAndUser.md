# Execution Checklist: Repo Work vs Server Work

## Summary
- Build the production refactor in two tracks: codebase changes inside this repo, and infrastructure/deployment work on the VPS.
- Complete all repo phases first up to local PostgreSQL validation, then provision the VPS, deploy, migrate data, and perform the first production login.
- Responsibility split:
  - `Repo / Codex`: application code, schema, migrations, auth, worker/job system, import logic, docs
  - `Server / You`: VPS provisioning, package installs, secrets, DNS/HTTPS, services, backups, first live login

## Repo Phase 1: Foundation
**Codex can do in this repo**
- Add production dependencies for PostgreSQL, ORM, migrations, password hashing, and session handling.
- Introduce environment-based config for DB URL, app secrets, cookie settings, host identity, and profile storage path.
- Create the initial PostgreSQL schema and Alembic migration set for:
  - `users`
  - `auth_sessions`
  - `workspaces`
  - `workspace_memberships`
  - `workspace_settings`
  - `worker_nodes`
  - `workspace_sessions`
  - `contacts`
  - `contact_sync_runs`
  - `campaigns`
  - `campaign_results`
  - `automation_jobs`
  - `automation_job_events`
  - `audit_logs`
- Replace the current global storage assumptions so the app can read from PostgreSQL instead of `contacts.sqlite3` and `settings.json`.

**Requires you on the server**
- Nothing yet, except deciding the real production secrets and hostname values later.

## Repo Phase 2: Auth and Workspace Model
**Codex can do in this repo**
- Implement local email/password authentication with hashed passwords and secure cookie sessions.
- Add current-user and current-workspace resolution in FastAPI.
- Add role-based authorization for `admin`, `operator`, `viewer`.
- Add these endpoints:
  - `POST /api/auth/login`
  - `POST /api/auth/logout`
  - `GET /api/auth/me`
  - `GET /api/workspaces`
  - `POST /api/workspaces/{workspace_id}/switch`
- Refactor all existing business routes so they require authenticated workspace context.

**Requires you on the server**
- Later: provide the production session secret and any admin bootstrap values.

## Repo Phase 3: Data Layer Refactor
**Codex can do in this repo**
- Replace `ContactStore` with PostgreSQL-backed repository/service code.
- Migrate settings reads/writes into `workspace_settings`.
- Migrate contacts, sync runs, campaigns, and campaign results into normalized PostgreSQL tables.
- Preserve current app behavior where practical while removing global process-wide data assumptions.

**Requires you on the server**
- Later: provision the actual PostgreSQL database where these migrations will run.

## Repo Phase 4: Durable Jobs and Worker Split
**Codex can do in this repo**
- Split the runtime into API process and worker process.
- Replace in-memory campaign progress and request-time execution with `automation_jobs` and `automation_job_events`.
- Add worker job claiming, lease renewal, heartbeat, cancellation checks, and failure classification.
- Add workspace session metadata tracking for persistent browser profiles.
- Update progress/status endpoints to read persisted state from PostgreSQL.

**Requires you on the server**
- Later: run separate API and worker services on the VPS.

## Repo Phase 5: Import, Ops, and Hardening
**Codex can do in this repo**
- Build one-time import code from current SQLite/settings into the first workspace.
- Add `/api/health` and `/api/readiness`.
- Restrict CORS for production configuration.
- Add audit logging for auth, settings changes, syncs, sends, and failures.
- Write deployment docs:
  - required packages
  - env vars
  - migration commands
  - service start commands
  - operational notes for VPS login/recovery

**Requires you on the server**
- Use the docs to set the real env vars, service files, and filesystem paths.

## Server Phase 1: VPS Provisioning
**Requires you on the server**
- Provision the Ubuntu VPS.
- Install:
  - `PostgreSQL`
  - `Python`
  - `Google Chrome`
  - Playwright Linux dependencies
  - `Caddy`
  - `Xvfb`
  - `noVNC`
- Create the application user and directories for:
  - app deployment
  - logs
  - browser profiles
  - backups
- Keep PostgreSQL bound to localhost and expose only `80/443`.

**Codex can support from the repo**
- Provide exact install checklist and service command templates, but cannot perform the VPS setup from inside this repo.

## Server Phase 2: Database and Secrets
**Requires you on the server**
- Create the PostgreSQL database and database user.
- Set production secrets:
  - DB URL
  - session secret
  - cookie/domain settings
  - host identity
  - profile storage path
- Prepare environment files or systemd environment configuration.

**Codex can support from the repo**
- Provide the required env var list and expected values/format.

## Server Phase 3: Deploy Application
**Requires you on the server**
- Pull/copy the repo to the VPS.
- Create the Python environment and install app dependencies.
- Run Playwright browser install commands.
- Run Alembic migrations against production PostgreSQL.
- Create and enable systemd services for:
  - API
  - worker
  - optional Xvfb wrapper
- Configure `Caddy` with your public domain and HTTPS.

**Codex can support from the repo**
- Provide startup commands, migration commands, and example service definitions.

## Server Phase 4: Production Bootstrap
**Requires you on the server**
- Bootstrap the first admin user.
- Create the first workspace if not created by bootstrap tooling.
- Run the one-time import from existing SQLite/settings data.
- Perform the first Zalo login on the VPS through the virtual desktop/noVNC flow.
- Verify the workspace session becomes authenticated and persists.

**Codex can support from the repo**
- Implement the bootstrap/import commands and the session status visibility.

## Server Phase 5: Validation and Operations
**Requires you on the server**
- Verify external access from outside your network via the public HTTPS hostname.
- Confirm PostgreSQL is not publicly reachable.
- Test service restart, VPS reboot recovery, and backup execution.
- Set recurring backups for:
  - PostgreSQL dumps
  - browser profile directories

**Codex can support from the repo**
- Add readiness checks, worker heartbeat reporting, and operator-visible status endpoints to make these checks possible.

## Acceptance Checklist
- Users can log in and switch workspaces.
- Workspace A cannot access Workspace B data.
- Contacts, settings, syncs, campaigns, and jobs all persist in PostgreSQL.
- Long-running operations execute through the worker, not inside request handlers.
- Job progress survives API/worker restart.
- VPS serves the app over HTTPS to users outside your local network.
- PostgreSQL remains private.
- Workspace browser session survives normal service restarts and fails cleanly when re-login is needed.

## Assumptions and Defaults
- Production target is one Ubuntu VPS.
- PostgreSQL runs on that VPS in v1.
- Auth is local email/password with secure cookies.
- Persistent Playwright browser profiles remain the session model.
- Codex handles codebase work; server provisioning and live deployment steps are performed by you.
