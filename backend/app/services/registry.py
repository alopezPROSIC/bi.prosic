"""Punto único para calcular (y cachear) los KPIs de un módulo."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..cache import Cache, make_key
from ..config import Settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiCard, KpiFilters, ModuleKpis, ModuleName, Period
from . import comisiones, compras, historico, mantenimiento, pronostico, rentas, ventas
from .common import Ctx, ModuleUnavailable, ScopeRestricted
from .history import get_history

log = logging.getLogger("prosic.kpis")

BUILDERS: dict[str, Callable[[Ctx], Awaitable[tuple[list, list, list]]]] = {
    "ventas": ventas.build,
    "comisiones": comisiones.build,
    "rentas": rentas.build,
    "historico": historico.build,
    "mantenimiento": mantenimiento.build,
    "compras": compras.build,
    "pronostico": pronostico.build,
}


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """«23:00» -> (23, 0); vacío o inválido -> None (actualización nocturna desactivada)."""
    try:
        h, m = (int(x) for x in value.strip().split(":"))
    except (AttributeError, ValueError):
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def next_sync(settings: Settings, now: datetime | None = None) -> datetime | None:
    """Próxima actualización nocturna (hora de México)."""
    hm = parse_hhmm(settings.NIGHTLY_SYNC_TIME)
    if not hm:
        return None
    now = now or datetime.now(ZoneInfo(settings.ODOO_TZ))
    run = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def seconds_until_sync(settings: Settings, now: datetime | None = None) -> int:
    nxt = next_sync(settings, now)
    if not nxt:
        return settings.KPI_CACHE_TTL_SECONDS
    now = now or datetime.now(ZoneInfo(settings.ODOO_TZ))
    return int((nxt - now).total_seconds()) + 60


# Una sola consulta a Odoo por vista: si N usuarios piden a la vez la misma vista con la caché fría, solo el primero
# consulta a Odoo y los demás esperan y leen su resultado de la caché. llave -> [candado, usuarios esperando]
_inflight: dict[str, list] = {}


async def get_module_kpis(odoo: OdooClient, cache: Cache, settings: Settings, module: ModuleName,
                          filters: KpiFilters, use_cache: bool = True, ttl: int | None = None) -> ModuleKpis:
    key = make_key("kpi", module, filters.model_dump(mode="json"))
    if not use_cache:
        return await _compute_module_kpis(odoo, cache, settings, module, filters, key, ttl)
    hit = await cache.get_json(key)
    if hit:
        return ModuleKpis.model_validate(hit)
    entry = _inflight.get(key)
    if entry is None:
        entry = _inflight[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            hit = await cache.get_json(key)  # otra petición pudo calcularla mientras esperábamos el candado
            if hit:
                return ModuleKpis.model_validate(hit)
            return await _compute_module_kpis(odoo, cache, settings, module, filters, key, ttl)
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _inflight.pop(key, None)


async def _compute_module_kpis(odoo: OdooClient, cache: Cache, settings: Settings, module: ModuleName,
                               filters: KpiFilters, key: str, ttl: int | None) -> ModuleKpis:
    ctx = Ctx(odoo=odoo, filters=filters, tz=settings.ODOO_TZ, history=get_history(settings.HISTORY_DB_PATH),
              settings=settings)
    prev = filters.previous()
    result = ModuleKpis(
        module=module, filters=filters,
        period=Period(date_from=filters.date_from, date_to=filters.date_to),
        previous_period=Period(date_from=prev.date_from, date_to=prev.date_to) if prev else None,
        generated_at=datetime.now(timezone.utc),
    )
    try:
        cards, charts, tables = await BUILDERS[module](ctx)
        result.cards, result.charts, result.tables = cards, charts, tables
    except (ModuleUnavailable, ScopeRestricted) as exc:
        result.available, result.reason = False, str(exc)
    result.notes = ctx.notes
    # Vigente hasta la actualización nocturna: durante el día no se vuelve a consultar Odoo para la misma vista.
    ttl = ttl or seconds_until_sync(settings)
    await cache.set_json(key, result.model_dump(mode="json"), max(ttl, settings.KPI_CACHE_TTL_SECONDS))
    return result


def card_index(kpis: ModuleKpis) -> dict[str, KpiCard]:
    return {c.key: c for c in kpis.cards}
