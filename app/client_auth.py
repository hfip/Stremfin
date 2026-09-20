"""Jellyfin / Emby client authentication helpers for Stremfin."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class ClientCredentials:
    """Normalized credentials received from a Jellyfin/Emby client."""

    username: str
    password: str


def client_auth_enabled(settings: Settings) -> bool:
    """Return whether protected Jellyfin/Emby client login is enabled."""
    return bool(settings.client_auth_enabled)


def configured_username(settings: Settings) -> str:
    """Return the configured client username with a safe compatibility default."""
    value = str(settings.client_username or "").strip()
    return value or "stremfin"


def has_configured_password(settings: Settings) -> bool:
    """Report password state without exposing the secret itself."""
    return bool(str(settings.client_password or ""))


def normalize_credentials(
    username: object,
    password: object,
) -> ClientCredentials:
    """Normalize values received from JSON login payloads."""
    return ClientCredentials(
        username=str(username or "").strip(),
        password=str(password or ""),
    )


def credentials_are_valid(
    settings: Settings,
    username: object,
    password: object,
) -> bool:
    """
    Validate Jellyfin/Emby client credentials.

    Compatibility mode:
        When CLIENT_AUTH_ENABLED is false, authentication remains permissive
        exactly like the original Stremfin behavior.

    Protected mode:
        Username and password must exactly match the configured values.
        compare_digest is used for both comparisons to avoid ordinary
        short-circuit string comparison of secrets.
    """
    if not client_auth_enabled(settings):
        return True

    supplied = normalize_credentials(username, password)
    expected_username = configured_username(settings)
    expected_password = str(settings.client_password or "")

    username_ok = hmac.compare_digest(
        supplied.username.encode("utf-8"),
        expected_username.encode("utf-8"),
    )
    password_ok = hmac.compare_digest(
        supplied.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )

    return username_ok and password_ok


def user_auth_flags(settings: Settings) -> dict[str, bool]:
    """
    Produce Jellyfin-compatible user password flags.

    EnableAutoLogin is disabled only when client authentication is enabled.
    """
    protected = client_auth_enabled(settings)
    password_set = protected and has_configured_password(settings)

    return {
        "HasPassword": password_set,
        "HasConfiguredPassword": password_set,
        "EnableAutoLogin": not protected,
    }
