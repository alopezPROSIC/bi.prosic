"""Tareas en segundo plano (un solo proceso de uvicorn):

- Precálculo: cada PREFETCH_MINUTES recalcula los KPIs del «mes en curso» (la vista por defecto del
  dashboard) para que carguen al instante. De paso guarda la foto diaria de flota en el historial.
- Resumen semanal: el día/hora configurados envía el correo (si SMTP está configurado), una vez por semana.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..cache import Cache
from ..config import Settings, runtime_settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiFilters
from .history import get_history
from .registry import get_module_kpis
from .summary import email_enabled, send_summary

log = logging.getLogger("prosic.scheduler")
MODULES = ("ventas", "rentas", "mantenimiento", "compras", "pronostico")


def default_filters(settings: Settings) -> KpiFilters:
    """Igual que el preset «Este mes» del frontend (misma llave de caché)."""
    today = datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
    return KpiFilters(date_from=today.replace(day=1), date_to=today, compare="previous")


async def prefetch(odoo: OdooClient, cache: Cache, settings: Settings) -> None:
    f = default_filters(settings)
    ttl = settings.PREFETCH_MINUTES * 60 + 180  # sigue vigente hasta la siguiente vuelta
    for module in MODULES:
        try:
            await get_module_kpis(odoo, cache, settings, module, f, use_cache=False, ttl=ttl)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            log.warning("Precálculo de %s falló: %s", module, exc)


async def maybe_send_summary(odoo: OdooClient, cache: Cache, settings: Settings) -> None:
    if not email_enabled(settings):
        return
    now = datetime.now(ZoneInfo(settings.ODOO_TZ))
    if now.weekday() != settings.SUMMARY_WEEKDAY or now.hour < settings.SUMMARY_HOUR:
        return
    history = get_history(settings.HISTORY_DB_PATH)
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    if not history.enabled or await history.get("summary:last_week") == week:
        return
    await history.set("summary:last_week", week)  # se marca antes: evita reenvíos si el SMTP falla en bucle
    try:
        await send_summary(odoo, cache, settings)
    except Exception as exc:  # noqa: BLE001
        log.error("No se pudo enviar el resumen semanal: %s", exc)


async def run(odoo: OdooClient, cache: Cache, settings: Settings) -> None:
    await asyncio.sleep(5)  # deja arrancar el servidor
    last_prefetch = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            s = runtime_settings()  # incluye lo editado en el panel de administración
            if s.PREFETCH_MINUTES > 0 and loop.time() - last_prefetch >= s.PREFETCH_MINUTES * 60:
                last_prefetch = loop.time()
                await prefetch(odoo, cache, s)
            await maybe_send_summary(odoo, cache, s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Error en el programador")
        await asyncio.sleep(60)
