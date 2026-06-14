# Deployment Guide

## VPS Packages

Target host: Ubuntu VPS.

Install on the server:

- PostgreSQL 16
- Python 3.12 and `venv`
- Google Chrome Stable
- Playwright Linux dependencies and browser binaries
- Caddy
- Xvfb
- noVNC

## Environment Variables

Use `.env.example` as the template. Set these values in production:

- `MMBZALO_DATABASE_URL`
- `MMBZALO_SECRET_KEY`
- `MMBZALO_ENCRYPTION_KEY`
- `MMBZALO_COOKIE_SECURE=true`
- `MMBZALO_COOKIE_DOMAIN=<your domain>`
- `MMBZALO_CORS_ALLOWED_ORIGINS=["https://<your domain>"]`
- `MMBZALO_HOST_IDENTITY=<unique worker id>`
- `MMBZALO_BROWSER_PROFILES_ROOT=/var/lib/mmbzalo/profiles`
- `MMBZALO_LOGIN_DISPLAY=:99`

## Production Steps

1. Clone the repo to the VPS.
2. Create a Python virtual environment.
3. Install dependencies from `requirements.txt`.
4. Run `playwright install`.
5. Create PostgreSQL database and user.
6. Configure the `.env` file.
7. Run `alembic upgrade head`.
8. Bootstrap the first admin:
   - `python scripts/bootstrap_admin.py ...`
9. Optionally import the current local SQLite/settings data:
   - `python scripts/import_legacy_data.py ...`
10. Start:
   - API: `uvicorn app.main:app --host 127.0.0.1 --port 8000`
   - Worker: `python scripts/run_worker.py`
11. Put Caddy in front of the API and expose only HTTPS.
12. Use Xvfb/noVNC for the first Zalo login and future recovery.

## Systemd Service Commands

API command:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Worker command:

```bash
python scripts/run_worker.py
```

## Backups

Back up both of these:

- PostgreSQL dumps
- Browser profiles directory at `MMBZALO_BROWSER_PROFILES_ROOT`

## Server Tasks You Must Do

- Provision the Ubuntu VPS
- Install OS packages and Chrome/Playwright dependencies
- Create PostgreSQL database/user
- Configure Caddy, DNS, firewall, and HTTPS
- Create systemd services
- Run migrations in production
- Perform the first interactive Zalo login through the VPS display
