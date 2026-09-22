"""LRU + TTL 缓存层（对标 Redis 的常用使用场景：热点数据缓存 + 过期淘汰）。

为什么自己写一层：本项目要演示的是"缓存带来的收益有多大、命中率怎么统计"，
所以把淘汰策略、过期时间、命中/未命中计数都显式实现出来，便于用数据说话。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Optional, Tuple


class LruTtlCache:
    def __init__(self, capacity: int = 128, ttl_seconds: float = 30.0):
        if capacity <= 0:
            raise ValueError("capacity 必须为正")
        self.capacity = capacity
        self.ttl = ttl_seconds
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expires_at, value = item
        if expires_at < time.time():
            del self._data[key]
            self.expirations += 1
            self.misses += 1
            return None
        self._data.move_to_end(key)          # 命中即刷新为最近使用
        self.hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (time.time() + self.ttl, value)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)   # 淘汰最久未使用的
            self.evictions += 1

    def invalidate(self, prefix: str = "") -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            del self._data[k]
        return len(keys)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "capacity": self.capacity,
            "ttl_seconds": self.ttl,
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "hit_rate": (self.hits / total) if total else 0.0,
        }


class CachedQuery:
    """把"查库"包一层缓存：命中就直接返回，未命中才真正查库并写缓存。"""

    def __init__(self, cache: LruTtlCache):
        self.cache = cache
        self.db_calls = 0

    def run(self, key: str, loader):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self.db_calls += 1
        value = loader()
        self.cache.set(key, value)
        return value
