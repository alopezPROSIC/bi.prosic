"""Tareas en segundo plano (un solo proceso de uvicorn).

Para no cargar a Odoo durante el día, se consulta UNA vez al día, a NIGHTLY_SYNC_TIME (23:00 hora de México,
cuando casi nadie usa Odoo):

1. Sincroniza el histórico de flota (cambios de estado nuevos; el cierre del día es a las 23:00).
2. Vacía el caché de indicadores.
3. Precalcula la vista por defecto («Este mes») del día siguiente, para que cargue al instante en la mañana.

Durante el día, cada vista se consulta a Odoo una sola vez y queda en caché hasta la siguiente actualización
nocturna (ver ``registry.seconds_until_sync``). Al arrancar solo se sincroniza la flota si el histórico no existe o
se saltó una noche. El resumen semanal por correo sigue su propio horario.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..cache import Cache
from ..config import Settings, runtime_settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiFilters
from . import fleet_history
from .history import get_history
from .registry import get_module_kpis, next_sync, seconds_until_sync
from .summary import email_enabled, send_summary

log = logging.getLogger("prosic.scheduler")
MODULES = ("ventas", "comisiones", "rentas", "mantenimiento", "compras", "pronostico")  # «historico» no consulta Odoo: no se precalcula


def default_filters(settings: Settings, day: date) -> KpiFilters:
    """Igual que el preset «Este mes» del frontend abierto ese día (misma llave de caché)."""
    return KpiFilters(date_from=day.replace(day=1), date_to=day, compare="previous")


def target_day(now: datetime) -> date:
    """Día cuya vista se precalcula: la corrida de la noche deja lista la mañana siguiente."""
    return now.date() + timedelta(days=1) if now.hour >= 12 else now.date()


async def prefetch(odoo: OdooClient, cache: Cache, settings: Settings, day: date) -> None:
    f = default_filters(settings, day)
    ttl = seconds_until_sync(settings) + 3600  # sigue vigente hasta después de la siguiente corrida
    for module in MODULES:
        try:
            await get_module_kpis(odoo, cache, settings, module, f, use_cache=False, ttl=ttl)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            log.warning("Precálculo de %s falló: %s", module, exc)


async def sync_fleet(odoo: OdooClient, settings: Settings) -> None:
    try:
        await fleet_history.sync(odoo, settings)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("No se pudo sincronizar el histórico de flota")


async def nightly(odoo: OdooClient, cache: Cache, settings: Settings) -> None:
    now = datetime.now(ZoneInfo(settings.ODOO_TZ))
    log.info("Actualización nocturna desde Odoo: inicio")
    await sync_fleet(odoo, settings)
    await cache.delete_prefix("kpi")
    await prefetch(odoo, cache, settings, target_day(now))
    log.info("Actualización nocturna desde Odoo: fin")


async def fleet_is_stale(settings: Settings) -> bool:
    """True si el histórico de flota nunca se ha construido o se saltó la última corrida nocturna."""
    store = fleet_history.get_fleet_store(settings.HISTORY_DB_PATH)
    if not store.enabled:
        return False
    last = (await asyncio.to_thread(store.meta)).get("last_sync")
    if not last:
        return True
    return datetime.now(ZoneInfo(settings.ODOO_TZ)) - datetime.fromisoformat(last) > timedelta(hours=26)


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
    s = runtime_settings()
    if next_sync(s) and await fleet_is_stale(s):
        await sync_fleet(odoo, s)  # primera vez, o el servidor estuvo apagado a la hora de la corrida
    nxt = next_sync(runtime_settings())
    while True:
        try:
            s = runtime_settings()  # incluye lo editado en el panel de administración
            now = datetime.now(ZoneInfo(s.ODOO_TZ))
            if nxt and now >= nxt:
                await nightly(odoo, cache, s)
            if nxt is None or now >= nxt:
                nxt = next_sync(s)
            await maybe_send_summary(odoo, cache, s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Error en el programador")
            nxt = next_sync(runtime_settings())
        await asyncio.sleep(60)
