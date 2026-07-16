"""LFU + TTL cache used as the cache-aside layer in front of the data backend."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


_MISSING = object()


class LFUCache:
    """Least-frequently-used cache with per-entry TTL expiry.

    Ties on frequency are broken by insertion order, so a stale one-hit entry is
    evicted before an equally-cold newer one.
    """

    def __init__(self, capacity: int = 128, ttl: float = 300.0) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._ttl = ttl
        self._lock = threading.Lock()
        self._values: dict[Any, Any] = {}
        self._freq: dict[Any, int] = {}
        self._stored_at: dict[Any, float] = {}
        self._seq: dict[Any, int] = {}
        self._counter = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def _expired(self, key: Any) -> bool:
        return (time.monotonic() - self._stored_at[key]) > self._ttl

    def _drop(self, key: Any) -> None:
        for table in (self._values, self._freq, self._stored_at, self._seq):
            table.pop(key, None)

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            if key not in self._values:
                self.misses += 1
                return default
            if self._expired(key):
                self._drop(key)
                self.expirations += 1
                self.misses += 1
                return default
            self._freq[key] += 1
            self.hits += 1
            return self._values[key]

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            if key not in self._values and len(self._values) >= self._capacity:
                victim = min(self._values, key=lambda k: (self._freq[k], self._seq[k]))
                self._drop(victim)
                self.evictions += 1
            self._counter += 1
            self._values[key] = value
            self._freq.setdefault(key, 0)
            self._stored_at[key] = time.monotonic()
            self._seq[key] = self._counter

    def get_or_load(self, key: Any, loader: Callable[[], Any]) -> Any:
        """Cache-aside read: return the cached value or load, store, and return it.

        The loader runs outside the lock so a slow backend query never blocks other
        readers; a concurrent duplicate load is acceptable and cheaper than holding
        the lock across network I/O.
        """
        found = self.get(key, _MISSING)
        if found is not _MISSING:
            return found
        value = loader()
        self.put(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._freq.clear()
            self._stored_at.clear()
            self._seq.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._values),
                "capacity": self._capacity,
                "ttl_seconds": self._ttl,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }
