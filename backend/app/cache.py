"""Caché en Redis con degradación elegante: si Redis no está disponible, el
servicio sigue funcionando sin caché (y lo registra en logs)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("prosic.cache")


def make_key(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return f"prosic:{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


class Cache:
    def __init__(self, url: str):
        # url vacía => solo memoria (útil en desarrollo local y pruebas)
        self._redis = aioredis.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3) if url else None
        self._mem: dict[str, tuple[float, str]] = {}  # respaldo en memoria si Redis cae
        self._warned = False
        self._down_until = 0.0  # circuito abierto: no reintentar Redis hasta este instante

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    def _redis_ok(self) -> bool:
        return self._redis is not None and time.time() >= self._down_until

    def _fallback(self, exc: Exception) -> None:
        self._down_until = time.time() + 30
        if not self._warned:
            log.warning("Redis no disponible (%s); usando caché en memoria del proceso", exc)
            self._warned = True

    async def get_json(self, key: str) -> Any | None:
        raw = None
        try:
            if not self._redis_ok():
                raise ConnectionError("circuito abierto")
            raw = await self._redis.get(key)
        except Exception as exc:  # noqa: BLE001
            if self._redis_ok():
                self._fallback(exc)
            item = self._mem.get(key)
            raw = item[1] if item and item[0] > time.time() else None
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        raw = json.dumps(value, default=str, ensure_ascii=False)
        try:
            if not self._redis_ok():
                raise ConnectionError("circuito abierto")
            await self._redis.set(key, raw, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            if self._redis_ok():
                self._fallback(exc)
            if len(self._mem) > 500:
                self._mem.clear()
            self._mem[key] = (time.time() + ttl, raw)

    async def incr_window(self, key: str, ttl: int) -> int:
        """Contador con ventana fija (para rate limiting)."""
        try:
            if not self._redis_ok():
                raise ConnectionError("circuito abierto")
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl, nx=True)
            count, _ = await pipe.execute()
            return int(count)
        except Exception as exc:  # noqa: BLE001
            if self._redis_ok():
                self._fallback(exc)
            exp, raw = self._mem.get(key, (time.time() + ttl, "0"))
            if exp < time.time():
                exp, raw = time.time() + ttl, "0"
            count = int(raw) + 1
            self._mem[key] = (exp, str(count))
            return count

    async def delete_prefix(self, prefix: str) -> int:
        n = 0
        try:
            if not self._redis_ok():
                raise ConnectionError("circuito abierto")
            async for k in self._redis.scan_iter(match=f"prosic:{prefix}:*", count=500):
                await self._redis.delete(k)
                n += 1
        except Exception as exc:  # noqa: BLE001
            if self._redis_ok():
                self._fallback(exc)
        for k in [k for k in self._mem if k.startswith(f"prosic:{prefix}:")]:
            self._mem.pop(k, None)
            n += 1
        return n

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False
