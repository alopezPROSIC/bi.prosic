"""Punto único para calcular (y cachear) los KPIs de un módulo."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

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


async def get_module_kpis(odoo: OdooClient, cache: Cache, settings: Settings, module: ModuleName,
                          filters: KpiFilters, use_cache: bool = True, ttl: int | None = None) -> ModuleKpis:
    key = make_key("kpi", module, filters.model_dump(mode="json"))
    if use_cache:
        hit = await cache.get_json(key)
        if hit:
            return ModuleKpis.model_validate(hit)

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
    await cache.set_json(key, result.model_dump(mode="json"), max(ttl or 0, settings.KPI_CACHE_TTL_SECONDS))
    return result


def card_index(kpis: ModuleKpis) -> dict[str, KpiCard]:
    return {c.key: c for c in kpis.cards}
