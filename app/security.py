from __future__ import annotations

import hashlib
import secrets
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone

from app.config import get_settings


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${120000}${b64encode(salt).decode('utf-8')}${b64encode(digest).decode('utf-8')}"


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = hashed_password.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = b64decode(salt_raw.encode("utf-8"))
        expected = b64decode(digest_raw.encode("utf-8"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


def generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def session_expiry() -> datetime:
    settings = get_settings()
    return utcnow() + timedelta(hours=settings.session_ttl_hours)
