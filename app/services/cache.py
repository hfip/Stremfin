"""Async LRU/TTL cache with stale-while-revalidate support."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


Loader = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class _Entry:
    """
    One cached value.

    fresh_until:
        Requests before this timestamp receive the cached value directly.

    stale_until:
        Requests after fresh_until but before stale_until may still receive
        the cached value immediately while Stremfin refreshes it in the
        background.

    This behaviour is particularly useful for Jellyfin clients such as
    VidHub and Infuse because browsing does not need to stop while a Stremio
    addon is being refreshed.
    """

    value: Any
    fresh_until: float
    stale_until: float
    created_at: float
    last_accessed_at: float


class AsyncTTLCache:
    """
    Small in-process asynchronous LRU cache.

    Features:

    - TTL caching
    - stale-while-revalidate
    - per-key request coalescing
    - LRU eviction
    - background refresh
    - stale fallback when an upstream addon temporarily fails
    - explicit invalidation
    - lightweight cache statistics

    The public get_or_set() API intentionally remains compatible with the
    original Stremfin cache so existing services do not need to change.
    """

    def __init__(
        self,
        ttl_seconds: int = 1800,
        maxsize: int = 512,
        stale_seconds: int = 3600,
        persistent_path: str | None = None,
        persistent_namespace: str = "default",
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.maxsize = max(1, int(maxsize))
        self.stale_seconds = max(0, int(stale_seconds))
        self.persistent_path = (
            str(persistent_path).strip()
            if persistent_path
            else None
        )
        self.persistent_namespace = str(
            persistent_namespace or "default"
        ).strip() or "default"
        self._persistent_ready = False
        self._persistent_init_lock = asyncio.Lock()

        self._items: OrderedDict[str, _Entry] = OrderedDict()

        # Protects the cache data structures themselves.
        self._lock = asyncio.Lock()

        # Only one foreground loader may execute for a missing key.
        self._key_locks: dict[str, asyncio.Lock] = {}

        # Background stale-while-revalidate tasks.
        self._refresh_tasks: dict[str, asyncio.Task] = {}

        # Lightweight counters useful later for diagnostics/dashboard.
        self._hits = 0
        self._stale_hits = 0
        self._misses = 0
        self._loads = 0
        self._load_errors = 0
        self._evictions = 0
        self._background_refreshes = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_set(
        self,
        key: str,
        loader: Loader,
    ) -> Any:
        """
        Return a cached value or load it.

        Behaviour:

        1. Fresh cache hit:
           Return immediately.

        2. Stale cache hit:
           Return the stale value immediately and refresh it in the
           background.

        3. Missing/fully expired cache entry:
           One request loads the value while concurrent requests for the
           same key wait for that same loader.

        4. Loader failure:
           If a stale value still exists, return it instead of making the
           Jellyfin browsing request fail.
        """

        cache_key = str(key)

        if not cache_key:
            return await loader()

        now = time.monotonic()

        async with self._lock:
            entry = self._items.get(cache_key)

            if entry is not None:
                entry.last_accessed_at = now
                self._items.move_to_end(cache_key)

                if entry.fresh_until > now:
                    self._hits += 1
                    return entry.value

                if entry.stale_until > now:
                    self._stale_hits += 1

                    self._schedule_refresh_locked(
                        cache_key,
                        loader,
                    )

                    return entry.value

                # Fully expired. Keep the object temporarily until the
                # foreground load completes so it can still be used as a
                # last-resort fallback if the addon request fails.
                expired_entry = entry
            else:
                expired_entry = None

        # L2 persistent cache: after a process/container restart the RAM cache
        # is empty, but recently cached metadata can be restored from SQLite
        # without contacting the Stremio addon again.
        persistent = await self._persistent_get(cache_key)

        if persistent is not None:
            value, remaining_fresh, remaining_stale = persistent
            async with self._lock:
                self._store_locked(
                    cache_key,
                    value,
                    fresh_seconds=remaining_fresh,
                    stale_seconds=remaining_stale,
                )
                restored = self._items.get(cache_key)
                if restored is not None:
                    if restored.fresh_until > time.monotonic():
                        self._hits += 1
                    else:
                        self._stale_hits += 1
                        self._schedule_refresh_locked(
                            cache_key,
                            loader,
                        )
            return value

        async with self._lock:
            self._misses += 1

            key_lock = self._key_locks.get(cache_key)

            if key_lock is None:
                key_lock = asyncio.Lock()
                self._key_locks[cache_key] = key_lock

        async with key_lock:
            # Another request may have populated the key while this request
            # was waiting for the per-key lock.
            now = time.monotonic()

            async with self._lock:
                current = self._items.get(cache_key)

                if current is not None:
                    current.last_accessed_at = now
                    self._items.move_to_end(cache_key)

                    if current.fresh_until > now:
                        self._hits += 1
                        self._cleanup_key_lock_locked(
                            cache_key,
                            key_lock,
                        )
                        return current.value

                    if current.stale_until > now:
                        self._stale_hits += 1

                        self._schedule_refresh_locked(
                            cache_key,
                            loader,
                        )

                        self._cleanup_key_lock_locked(
                            cache_key,
                            key_lock,
                        )

                        return current.value

            try:
                value = await self._run_loader(loader)

            except Exception:
                async with self._lock:
                    self._load_errors += 1

                    fallback = self._items.get(cache_key)

                    self._cleanup_key_lock_locked(
                        cache_key,
                        key_lock,
                    )

                # A network/addon error should not destroy browsing if we
                # have any previously cached value available.
                if fallback is not None:
                    return fallback.value

                if expired_entry is not None:
                    return expired_entry.value

                raise

            async with self._lock:
                self._store_locked(
                    cache_key,
                    value,
                )

                self._cleanup_key_lock_locked(
                    cache_key,
                    key_lock,
                )

            await self._persistent_set(
                cache_key,
                value,
            )

            return value

    async def get(
        self,
        key: str,
        allow_stale: bool = True,
    ) -> Any | None:
        """
        Read a value without invoking a loader.

        Primarily useful for future prewarming and diagnostics.
        """

        cache_key = str(key)
        now = time.monotonic()

        async with self._lock:
            entry = self._items.get(cache_key)

            if entry is None:
                pass
            elif entry.fresh_until > now:
                entry.last_accessed_at = now
                self._items.move_to_end(cache_key)
                self._hits += 1
                return entry.value

            elif allow_stale and entry.stale_until > now:
                entry.last_accessed_at = now
                self._items.move_to_end(cache_key)
                self._stale_hits += 1
                return entry.value

            elif entry is not None and entry.stale_until <= now:
                self._items.pop(cache_key, None)

        persistent = await self._persistent_get(cache_key)
        if persistent is None:
            return None

        value, remaining_fresh, remaining_stale = persistent
        if not allow_stale and remaining_fresh <= 0:
            return None

        async with self._lock:
            self._store_locked(
                cache_key,
                value,
                fresh_seconds=remaining_fresh,
                stale_seconds=remaining_stale,
            )
            if remaining_fresh > 0:
                self._hits += 1
            else:
                self._stale_hits += 1
        return value

    async def set(
        self,
        key: str,
        value: Any,
    ) -> None:
        """Store a value directly."""

        cache_key = str(key)

        if not cache_key:
            return

        async with self._lock:
            self._store_locked(
                cache_key,
                value,
            )

        await self._persistent_set(
            cache_key,
            value,
        )

    async def invalidate(
        self,
        key: str,
    ) -> bool:
        """
        Remove one cache key.

        Returns True if the key existed.
        """

        cache_key = str(key)

        async with self._lock:
            existed = cache_key in self._items

            self._items.pop(
                cache_key,
                None,
            )

            task = self._refresh_tasks.pop(
                cache_key,
                None,
            )

            if task is not None and not task.done():
                task.cancel()

        persistent_existed = await self._persistent_delete(cache_key)
        return existed or persistent_existed

    async def invalidate_prefix(
        self,
        prefix: str,
    ) -> int:
        """
        Remove every key beginning with prefix.

        Useful when addon configuration or selected catalogs change.
        """

        value = str(prefix)

        async with self._lock:
            keys = [
                key
                for key in self._items
                if key.startswith(value)
            ]

            for key in keys:
                self._items.pop(
                    key,
                    None,
                )

                task = self._refresh_tasks.pop(
                    key,
                    None,
                )

                if task is not None and not task.done():
                    task.cancel()

            memory_count = len(keys)

        persistent_count = await self._persistent_delete_prefix(value)
        return max(memory_count, persistent_count)

    async def clear(self) -> None:
        """Clear all cached values and cancel background refreshes."""

        async with self._lock:
            tasks = list(
                self._refresh_tasks.values()
            )

            self._refresh_tasks.clear()
            self._items.clear()
            self._key_locks.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        await self._persistent_clear()

    async def stats(self) -> dict[str, int]:
        """Return lightweight cache statistics."""

        async with self._lock:
            return {
                "size": len(self._items),
                "maxsize": self.maxsize,
                "ttl_seconds": self.ttl_seconds,
                "stale_seconds": self.stale_seconds,
                "hits": self._hits,
                "stale_hits": self._stale_hits,
                "misses": self._misses,
                "loads": self._loads,
                "load_errors": self._load_errors,
                "evictions": self._evictions,
                "background_refreshes": (
                    self._background_refreshes
                ),
                "active_refreshes": sum(
                    1
                    for task in self._refresh_tasks.values()
                    if not task.done()
                ),
            }

    # ------------------------------------------------------------------
    # Loader / refresh internals
    # ------------------------------------------------------------------

    async def _run_loader(
        self,
        loader: Loader,
    ) -> Any:
        async with self._lock:
            self._loads += 1

        return await loader()

    def _schedule_refresh_locked(
        self,
        key: str,
        loader: Loader,
    ) -> None:
        """
        Start one background refresh for a stale key.

        Must be called while self._lock is held.
        """

        existing = self._refresh_tasks.get(key)

        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._refresh(
                key,
                loader,
            )
        )

        self._refresh_tasks[key] = task
        self._background_refreshes += 1

    async def _refresh(
        self,
        key: str,
        loader: Loader,
    ) -> None:
        """
        Refresh a stale key without blocking the request that discovered it.
        """

        try:
            value = await self._run_loader(loader)

        except asyncio.CancelledError:
            raise

        except Exception:
            # stale-while-revalidate deliberately keeps the existing value
            # when the upstream addon temporarily fails.
            async with self._lock:
                self._load_errors += 1

            return

        else:
            async with self._lock:
                self._store_locked(
                    key,
                    value,
                )

            await self._persistent_set(
                key,
                value,
            )

        finally:
            async with self._lock:
                current_task = asyncio.current_task()

                if (
                    self._refresh_tasks.get(key)
                    is current_task
                ):
                    self._refresh_tasks.pop(
                        key,
                        None,
                    )

    # ------------------------------------------------------------------
    # Storage internals
    # ------------------------------------------------------------------

    def _store_locked(
        self,
        key: str,
        value: Any,
        fresh_seconds: float | None = None,
        stale_seconds: float | None = None,
    ) -> None:
        """
        Store one entry and enforce LRU maxsize.

        Must be called while self._lock is held.
        """

        now = time.monotonic()

        fresh_for = (
            self.ttl_seconds
            if fresh_seconds is None
            else max(0.0, float(fresh_seconds))
        )
        stale_for = (
            self.stale_seconds
            if stale_seconds is None
            else max(0.0, float(stale_seconds))
        )

        fresh_until = now + fresh_for
        stale_until = fresh_until + stale_for

        self._items[key] = _Entry(
            value=value,
            fresh_until=fresh_until,
            stale_until=stale_until,
            created_at=now,
            last_accessed_at=now,
        )

        self._items.move_to_end(key)

        while len(self._items) > self.maxsize:
            evicted_key, _ = self._items.popitem(
                last=False
            )

            self._evictions += 1

            refresh_task = self._refresh_tasks.pop(
                evicted_key,
                None,
            )

            if (
                refresh_task is not None
                and not refresh_task.done()
            ):
                refresh_task.cancel()

    # ------------------------------------------------------------------
    # Persistent L2 SQLite cache
    # ------------------------------------------------------------------

    async def _ensure_persistent_ready(self) -> bool:
        if not self.persistent_path:
            return False
        if self._persistent_ready:
            return True

        async with self._persistent_init_lock:
            if self._persistent_ready:
                return True
            try:
                await asyncio.to_thread(self._persistent_init_sync)
            except Exception:
                # Persistence is an optimization only. Never make browsing
                # fail because SQLite is unavailable or temporarily locked.
                return False
            self._persistent_ready = True
            return True

    def _persistent_connect_sync(self) -> sqlite3.Connection:
        path = str(self.persistent_path)
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        db = sqlite3.connect(path, timeout=5.0)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _persistent_init_sync(self) -> None:
        with self._persistent_connect_sync() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS async_cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    fresh_until REAL NOT NULL,
                    stale_until REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(namespace, cache_key)
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_async_cache_expiry
                ON async_cache_entries(namespace, stale_until)
                """
            )

    async def _persistent_get(
        self,
        key: str,
    ) -> tuple[Any, float, float] | None:
        if not await self._ensure_persistent_ready():
            return None
        try:
            return await asyncio.to_thread(
                self._persistent_get_sync,
                key,
            )
        except Exception:
            return None

    def _persistent_get_sync(
        self,
        key: str,
    ) -> tuple[Any, float, float] | None:
        now = time.time()
        with self._persistent_connect_sync() as db:
            row = db.execute(
                """
                SELECT value_json, fresh_until, stale_until
                FROM async_cache_entries
                WHERE namespace = ? AND cache_key = ?
                """,
                (self.persistent_namespace, key),
            ).fetchone()

            if row is None:
                return None

            value_json, fresh_until, stale_until = row
            if float(stale_until) <= now:
                db.execute(
                    """
                    DELETE FROM async_cache_entries
                    WHERE namespace = ? AND cache_key = ?
                    """,
                    (self.persistent_namespace, key),
                )
                return None

            try:
                value = json.loads(value_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                db.execute(
                    """
                    DELETE FROM async_cache_entries
                    WHERE namespace = ? AND cache_key = ?
                    """,
                    (self.persistent_namespace, key),
                )
                return None

            remaining_fresh = max(0.0, float(fresh_until) - now)
            remaining_total = max(0.0, float(stale_until) - now)
            remaining_stale = max(
                0.0,
                remaining_total - remaining_fresh,
            )
            return value, remaining_fresh, remaining_stale

    async def _persistent_set(
        self,
        key: str,
        value: Any,
    ) -> None:
        if not await self._ensure_persistent_ready():
            return
        try:
            value_json = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return
        try:
            await asyncio.to_thread(
                self._persistent_set_sync,
                key,
                value_json,
            )
        except Exception:
            return

    def _persistent_set_sync(
        self,
        key: str,
        value_json: str,
    ) -> None:
        now = time.time()
        fresh_until = now + self.ttl_seconds
        stale_until = fresh_until + self.stale_seconds
        with self._persistent_connect_sync() as db:
            db.execute(
                """
                INSERT INTO async_cache_entries(
                    namespace,
                    cache_key,
                    value_json,
                    fresh_until,
                    stale_until,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, cache_key)
                DO UPDATE SET
                    value_json = excluded.value_json,
                    fresh_until = excluded.fresh_until,
                    stale_until = excluded.stale_until,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persistent_namespace,
                    key,
                    value_json,
                    fresh_until,
                    stale_until,
                    now,
                ),
            )

    async def _persistent_delete(self, key: str) -> bool:
        if not await self._ensure_persistent_ready():
            return False
        try:
            return bool(
                await asyncio.to_thread(
                    self._persistent_delete_sync,
                    key,
                )
            )
        except Exception:
            return False

    def _persistent_delete_sync(self, key: str) -> int:
        with self._persistent_connect_sync() as db:
            cursor = db.execute(
                """
                DELETE FROM async_cache_entries
                WHERE namespace = ? AND cache_key = ?
                """,
                (self.persistent_namespace, key),
            )
            return int(cursor.rowcount or 0)

    async def _persistent_delete_prefix(self, prefix: str) -> int:
        if not await self._ensure_persistent_ready():
            return 0
        try:
            return int(
                await asyncio.to_thread(
                    self._persistent_delete_prefix_sync,
                    prefix,
                )
            )
        except Exception:
            return 0

    def _persistent_delete_prefix_sync(self, prefix: str) -> int:
        with self._persistent_connect_sync() as db:
            cursor = db.execute(
                """
                DELETE FROM async_cache_entries
                WHERE namespace = ? AND cache_key LIKE ?
                """,
                (self.persistent_namespace, f"{prefix}%"),
            )
            return int(cursor.rowcount or 0)

    async def _persistent_clear(self) -> None:
        if not await self._ensure_persistent_ready():
            return
        try:
            await asyncio.to_thread(self._persistent_clear_sync)
        except Exception:
            return

    def _persistent_clear_sync(self) -> None:
        with self._persistent_connect_sync() as db:
            db.execute(
                """
                DELETE FROM async_cache_entries
                WHERE namespace = ?
                """,
                (self.persistent_namespace,),
            )

    def _cleanup_key_lock_locked(
        self,
        key: str,
        lock: asyncio.Lock,
    ) -> None:
        """
        Remove a per-key lock only when it is still the lock registered for
        that key.

        Must be called while self._lock is held.
        """

        if self._key_locks.get(key) is lock:
            self._key_locks.pop(
                key,
                None,
            )


# ---------------------------------------------------------------------------
# Shared Stremfin metadata cache
# ---------------------------------------------------------------------------

# Catalog browsing benefits from a relatively long cache lifetime because
# movie/series metadata does not need second-by-second freshness.
#
# Fresh for 30 minutes.
# Stale responses may be served for another 60 minutes while refreshing.
# 1024 entries leaves enough room for:
#
# - manifests
# - catalog pages
# - movie metadata
# - series metadata
#
# without allowing unbounded process memory growth.

metadata_cache = AsyncTTLCache(
    ttl_seconds=1800,
    stale_seconds=3600,
    maxsize=1024,
    persistent_path=os.getenv(
        "DATABASE_PATH",
        "./data/stremfin.db",
    ),
    persistent_namespace="metadata",
)
