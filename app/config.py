"""Environment-backed application settings."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Stremfin"
    app_version: str = "0.3.0"
    server_name: str = "Stremfin Jellyfin Bridge"
    server_id: str = "stremfin-local"
    public_base_url: str = "http://localhost:3000"

    stremio_addon_url: str | None = None
    stremio_addon_urls: str = ""
    subtitle_addon_urls: str = ""

    debrid_provider: str = "none"
    real_debrid_api_key: str | None = None
    torbox_api_key: str | None = None

    # Optional TMDB metadata provider.
    # Client-requested language will take priority when TMDB is wired into
    # metadata.py. This value is only the server-side fallback locale.
    tmdb_api_key: str = ""
    tmdb_access_token: str = ""
    tmdb_default_language: str = "en-US"

    database_path: str = "./data/stremfin.db"

    dashboard_username: str = "admin"
    dashboard_password: str = "admin"
    dashboard_session_secret: str = "change-me-in-production"

    client_auth_enabled: bool = False
    client_username: str = "stremfin"
    client_password: str = ""

    request_timeout_seconds: float = 20.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def addon_urls(self) -> list[str]:
        values = [self.stremio_addon_url] if self.stremio_addon_url else []
        values.extend(item.strip() for item in self.stremio_addon_urls.split(","))
        return [
            item.removesuffix("/manifest.json").rstrip("/")
            for item in values
            if item.strip()
        ]

    @property
    def tmdb_enabled(self) -> bool:
        return bool(
            self.tmdb_api_key.strip()
            or self.tmdb_access_token.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
