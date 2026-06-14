from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode("utf-8"))


def encrypt_value(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
