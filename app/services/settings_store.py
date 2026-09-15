"""Small SQLite settings repository used by the dashboard and API."""
import json
import sqlite3
from pathlib import Path
from threading import Lock
from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    debrid_provider: str = "none"
    debrid_api_key: str = ""
    stremio_addon_urls: list[str] = Field(default_factory=list)
    tmdb_api_key: str = ""
    preferred_resolutions: list[str] = Field(default_factory=lambda: ["1080p", "4K"])
    preferred_audio_formats: list[str] = Field(default_factory=lambda: ["EAC3", "AAC", "AC3"])


class SettingsStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init_db(self):
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def load(self) -> AppSettings:
        with self._lock, self._connect() as db:
            rows = dict(db.execute("SELECT key, value FROM settings"))
        values = {}
        for key, raw in rows.items():
            try:
                values[key] = json.loads(raw)
            except json.JSONDecodeError:
                values[key] = raw
        return AppSettings.model_validate(values)

    def save(self, settings: AppSettings) -> AppSettings:
        payload = settings.model_dump()
        with self._lock, self._connect() as db:
            db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [(key, json.dumps(value)) for key, value in payload.items()])
        return settings
