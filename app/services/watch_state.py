"""Persistent Jellyfin / Emby watch-state storage for Stremfin."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TICKS_PER_SECOND = 10_000_000


@dataclass(frozen=True, slots=True)
class WatchState:
    user_id: str
    item_id: str
    position_ticks: int = 0
    runtime_ticks: int = 0
    media_source_id: str | None = None
    played: bool = False
    play_count: int = 0
    last_played_date: str | None = None
    updated_at: str | None = None

    @property
    def progress_percent(self) -> float:
        if self.runtime_ticks <= 0:
            return 0.0
        return max(
            0.0,
            min(
                100.0,
                (self.position_ticks / self.runtime_ticks) * 100.0,
            ),
        )

    def jellyfin_user_data(self) -> dict[str, Any]:
        return {
            "PlaybackPositionTicks": max(0, self.position_ticks),
            "PlayCount": max(0, self.play_count),
            "IsFavorite": False,
            "Played": bool(self.played),
            "UnplayedItemCount": 0,
        }


class WatchStateStore:
    """SQLite-backed playback progress with async-safe database access."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def ensure_ready(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._init_db)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=5.0,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS watch_state (
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    position_ticks INTEGER NOT NULL DEFAULT 0,
                    runtime_ticks INTEGER NOT NULL DEFAULT 0,
                    media_source_id TEXT,
                    played INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    last_played_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, item_id)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(watch_state)").fetchall()
            }
            if "media_source_id" not in columns:
                db.execute(
                    "ALTER TABLE watch_state ADD COLUMN media_source_id TEXT"
                )

            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_watch_state_resume
                ON watch_state(user_id, played, position_ticks, updated_at)
                """
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _clean_ticks(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _row_to_state(row: sqlite3.Row | None) -> WatchState | None:
        if row is None:
            return None
        return WatchState(
            user_id=str(row["user_id"]),
            item_id=str(row["item_id"]),
            position_ticks=int(row["position_ticks"] or 0),
            runtime_ticks=int(row["runtime_ticks"] or 0),
            media_source_id=(
                str(row["media_source_id"])
                if row["media_source_id"]
                else None
            ),
            played=bool(row["played"]),
            play_count=int(row["play_count"] or 0),
            last_played_date=row["last_played_date"],
            updated_at=row["updated_at"],
        )

    async def get(self, user_id: str, item_id: str) -> WatchState | None:
        await self.ensure_ready()
        return await asyncio.to_thread(self._get_sync, user_id, item_id)

    def _get_sync(self, user_id: str, item_id: str) -> WatchState | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM watch_state
                WHERE user_id = ? AND item_id = ?
                """,
                (user_id, item_id),
            ).fetchone()
        return self._row_to_state(row)

    async def update_progress(
        self,
        user_id: str,
        item_id: str,
        position_ticks: Any,
        runtime_ticks: Any = 0,
        media_source_id: str | None = None,
    ) -> WatchState:
        await self.ensure_ready()
        return await asyncio.to_thread(
            self._update_progress_sync,
            user_id,
            item_id,
            self._clean_ticks(position_ticks),
            self._clean_ticks(runtime_ticks),
            str(media_source_id or "").strip() or None,
        )

    def _update_progress_sync(
        self,
        user_id: str,
        item_id: str,
        position_ticks: int,
        runtime_ticks: int,
        media_source_id: str | None,
    ) -> WatchState:
        now = self._utc_now()

        # Jellyfin convention: near-complete playback counts as played.
        completed = bool(
            runtime_ticks > 0
            and position_ticks >= int(runtime_ticks * 0.90)
        )
        stored_position = 0 if completed else position_ticks

        with self._connect() as db:
            previous = db.execute(
                """
                SELECT played, play_count, runtime_ticks, media_source_id
                FROM watch_state
                WHERE user_id = ? AND item_id = ?
                """,
                (user_id, item_id),
            ).fetchone()

            old_played = bool(previous["played"]) if previous else False
            play_count = int(previous["play_count"] or 0) if previous else 0
            previous_runtime = (
                int(previous["runtime_ticks"] or 0)
                if previous
                else 0
            )
            effective_runtime = runtime_ticks or previous_runtime
            previous_media_source_id = (
                str(previous["media_source_id"])
                if previous and previous["media_source_id"]
                else None
            )
            effective_media_source_id = (
                media_source_id or previous_media_source_id
            )

            if completed and not old_played:
                play_count += 1

            db.execute(
                """
                INSERT INTO watch_state(
                    user_id,
                    item_id,
                    position_ticks,
                    runtime_ticks,
                    media_source_id,
                    played,
                    play_count,
                    last_played_date,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, item_id)
                DO UPDATE SET
                    position_ticks = excluded.position_ticks,
                    runtime_ticks = excluded.runtime_ticks,
                    media_source_id = excluded.media_source_id,
                    played = excluded.played,
                    play_count = excluded.play_count,
                    last_played_date = excluded.last_played_date,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    item_id,
                    stored_position,
                    effective_runtime,
                    effective_media_source_id,
                    int(completed),
                    play_count,
                    now,
                    now,
                ),
            )

            row = db.execute(
                """
                SELECT *
                FROM watch_state
                WHERE user_id = ? AND item_id = ?
                """,
                (user_id, item_id),
            ).fetchone()

        state = self._row_to_state(row)
        if state is None:
            raise RuntimeError("Failed to persist watch state")
        return state

    async def mark_played(
        self,
        user_id: str,
        item_id: str,
        runtime_ticks: Any = 0,
    ) -> WatchState:
        await self.ensure_ready()
        return await asyncio.to_thread(
            self._mark_played_sync,
            user_id,
            item_id,
            self._clean_ticks(runtime_ticks),
        )

    def _mark_played_sync(
        self,
        user_id: str,
        item_id: str,
        runtime_ticks: int,
    ) -> WatchState:
        now = self._utc_now()
        with self._connect() as db:
            previous = db.execute(
                """
                SELECT played, play_count, runtime_ticks
                FROM watch_state
                WHERE user_id = ? AND item_id = ?
                """,
                (user_id, item_id),
            ).fetchone()

            already_played = bool(previous["played"]) if previous else False
            play_count = int(previous["play_count"] or 0) if previous else 0
            previous_runtime = (
                int(previous["runtime_ticks"] or 0)
                if previous
                else 0
            )
            effective_runtime = runtime_ticks or previous_runtime

            if not already_played:
                play_count += 1

            db.execute(
                """
                INSERT INTO watch_state(
                    user_id, item_id, position_ticks, runtime_ticks,
                    played, play_count, last_played_date, updated_at
                )
                VALUES(?, ?, 0, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id, item_id)
                DO UPDATE SET
                    position_ticks = 0,
                    runtime_ticks = excluded.runtime_ticks,
                    played = 1,
                    play_count = excluded.play_count,
                    last_played_date = excluded.last_played_date,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    item_id,
                    effective_runtime,
                    play_count,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM watch_state WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            ).fetchone()

        state = self._row_to_state(row)
        if state is None:
            raise RuntimeError("Failed to persist played state")
        return state

    async def mark_unplayed(self, user_id: str, item_id: str) -> WatchState:
        await self.ensure_ready()
        return await asyncio.to_thread(
            self._mark_unplayed_sync,
            user_id,
            item_id,
        )

    def _mark_unplayed_sync(
        self,
        user_id: str,
        item_id: str,
    ) -> WatchState:
        now = self._utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO watch_state(
                    user_id, item_id, position_ticks, runtime_ticks,
                    played, play_count, last_played_date, updated_at
                )
                VALUES(?, ?, 0, 0, 0, 0, NULL, ?)
                ON CONFLICT(user_id, item_id)
                DO UPDATE SET
                    position_ticks = 0,
                    played = 0,
                    updated_at = excluded.updated_at
                """,
                (user_id, item_id, now),
            )
            row = db.execute(
                "SELECT * FROM watch_state WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            ).fetchone()

        state = self._row_to_state(row)
        if state is None:
            raise RuntimeError("Failed to persist unplayed state")
        return state

    async def resume(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WatchState]:
        await self.ensure_ready()
        return await asyncio.to_thread(
            self._resume_sync,
            user_id,
            max(1, int(limit)),
            max(0, int(offset)),
        )

    def _resume_sync(
        self,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[WatchState]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT *
                FROM watch_state
                WHERE user_id = ?
                  AND played = 0
                  AND position_ticks > 0
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
        return [
            state
            for row in rows
            if (state := self._row_to_state(row)) is not None
        ]

    async def resume_count(self, user_id: str) -> int:
        await self.ensure_ready()
        return await asyncio.to_thread(self._resume_count_sync, user_id)

    def _resume_count_sync(self, user_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*)
                FROM watch_state
                WHERE user_id = ?
                  AND played = 0
                  AND position_ticks > 0
                """,
                (user_id,),
            ).fetchone()
        return int(row[0] if row else 0)
