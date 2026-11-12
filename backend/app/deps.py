"""Dependencias compartidas de FastAPI."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status

from .auth import User, current_user
from .cache import Cache
from .config import Settings, runtime_settings
from .odoo_client import OdooClient


def get_odoo(request: Request) -> OdooClient:
    return request.app.state.odoo


def get_cache(request: Request) -> Cache:
    return request.app.state.cache


async def ai_rate_limit(user: User = Depends(current_user), cache: Cache = Depends(get_cache),
                        settings: Settings = Depends(runtime_settings)) -> User:
    now = datetime.now(timezone.utc)
    minute = await cache.incr_window(f"prosic:rl:min:{user.username}:{now:%Y%m%d%H%M}", 70)
    day = await cache.incr_window(f"prosic:rl:day:{user.username}:{now:%Y%m%d}", 90000)
    if minute > settings.AI_RATE_LIMIT_PER_MINUTE or day > settings.AI_RATE_LIMIT_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Límite de solicitudes de IA alcanzado. Intenta más tarde.",
                            headers={"Retry-After": "60"})
    return user
