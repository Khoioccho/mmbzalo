from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class ParsedProxyConfig:
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    def to_playwright_proxy(self) -> dict:
        proxy = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy


def _parse_port(raw_port: str) -> int:
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Proxy port must be a number.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Proxy port must be between 1 and 65535.")
    return port


def _build_config(
    *,
    scheme: str,
    host: str,
    port: str | int,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> ParsedProxyConfig:
    normalized_scheme = (scheme or "http").strip().lower()
    if normalized_scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Only HTTP/HTTPS proxies are supported.")

    normalized_host = (host or "").strip()
    if not normalized_host:
        raise ValueError("Proxy host is required.")

    normalized_username = username.strip() if username else None
    normalized_password = password.strip() if password else None
    if normalized_password and not normalized_username:
        raise ValueError("Proxy username is required when a password is provided.")

    return ParsedProxyConfig(
        scheme=normalized_scheme,
        host=normalized_host,
        port=_parse_port(str(port).strip()),
        username=normalized_username or None,
        password=normalized_password or None,
    )


def parse_proxy_input(proxy_raw: str) -> ParsedProxyConfig:
    value = (proxy_raw or "").strip()
    if not value:
        raise ValueError("Proxy value is required when proxy is enabled.")

    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.hostname or not parsed.port:
            raise ValueError("Proxy must include both hostname and port.")
        return _build_config(
            scheme=parsed.scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    if "@" in value:
        left, right = value.rsplit("@", 1)
        left_parts = left.split(":")
        right_parts = right.split(":")

        if len(left_parts) == 2 and left_parts[1].isdigit():
            host, port = left_parts
            username = right_parts[0] if right_parts else None
            password = right_parts[1] if len(right_parts) > 1 else None
            return _build_config(
                scheme="http",
                host=host,
                port=port,
                username=username,
                password=password,
            )

        if len(right_parts) == 2 and right_parts[1].isdigit():
            host, port = right_parts
            username = left_parts[0] if left_parts else None
            password = left_parts[1] if len(left_parts) > 1 else None
            return _build_config(
                scheme="http",
                host=host,
                port=port,
                username=username,
                password=password,
            )

        raise ValueError("Proxy must include one hostname:port segment.")

    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
        return _build_config(scheme="http", host=host, port=port)
    if len(parts) == 4:
        first, second, third, fourth = parts
        if second.isdigit():
            return _build_config(
                scheme="http",
                host=first,
                port=second,
                username=third,
                password=fourth,
            )
        if fourth.isdigit():
            return _build_config(
                scheme="http",
                host=third,
                port=fourth,
                username=first,
                password=second,
            )
        raise ValueError("Proxy must include one numeric port segment.")

    raise ValueError("Unsupported proxy format.")


def parse_proxy_settings(
    *,
    proxy_enabled: bool,
    proxy_raw: Optional[str],
    proxy_address: Optional[str] = None,
    proxy_port: Optional[int] = None,
) -> Optional[ParsedProxyConfig]:
    if not proxy_enabled:
        return None

    if proxy_raw and proxy_raw.strip():
        return parse_proxy_input(proxy_raw)

    if proxy_address and proxy_port:
        return _build_config(scheme="http", host=proxy_address, port=proxy_port)

    raise ValueError("Proxy is enabled but no valid proxy value was provided.")
