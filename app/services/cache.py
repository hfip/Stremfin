"""Small async LRU/TTL cache for live addon responses."""
from collections import OrderedDict
from dataclasses import dataclass
import asyncio
import time
from typing import Any, Awaitable, Callable


@dataclass
class _Entry:
    value: Any
    expires_at: float


class AsyncTTLCache:
    def __init__(self, ttl_seconds: int = 1800, maxsize: int = 256):
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize
        self._items: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def get_or_set(self, key: str, loader: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        async with self._lock:
            entry = self._items.get(key)
            if entry and entry.expires_at > now:
                self._items.move_to_end(key)
                return entry.value
            if entry:
                self._items.pop(key, None)
            key_lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with key_lock:
            async with self._lock:
                entry = self._items.get(key)
                if entry and entry.expires_at > time.monotonic():
                    self._items.move_to_end(key)
                    return entry.value
            value = await loader()
            async with self._lock:
                self._items[key] = _Entry(value, time.monotonic() + self.ttl_seconds)
                self._items.move_to_end(key)
                while len(self._items) > self.maxsize:
                    self._items.popitem(last=False)
                self._key_locks.pop(key, None)
            return value

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()


metadata_cache = AsyncTTLCache()
