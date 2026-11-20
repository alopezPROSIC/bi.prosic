"""Ocupación diaria de la flota de renta (tacómetro): lectura de la mañana, del momento y cierre de la noche.

Reemplaza la imagen que Contabilidad enviaba cada mañana y cada noche. Usa la MISMA definición que «Rentas e
inventario» y el «Histórico de flota»:

    ocupación = unidades en estado ocupado (FLEET_OCCUPIED_STATES: Rentado, Rentado - Servicio, Por entregar)
                / unidades de la flota de renta (sin FLEET_EXCLUDED_STATES)

- Cierre de la noche: el estado de cada equipo a las 23:00 (FLEET_DAY_CLOSE_HOUR), del histórico local.
- Corte de la mañana: una lectura de Odoo a OCCUPANCY_MORNING_TIME (08:00) que se guarda en la base local.
- Ahora: lectura de Odoo con caché de OCCUPANCY_LIVE_MINUTES (una consulta ligera para todos los usuarios).
  Si Odoo no responde se muestra la última lectura guardada; nunca se insiste.

Solo lectura sobre Odoo (``fleet.vehicle``: estado y almacén actual de los equipos de la flota de renta).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..cache import Cache
from ..config import Settings
from ..odoo_client import OdooClient
from .fleet_history import STATE_GROUPS, get_fleet_store, state_label
from .historico import resolve_states

log = logging.getLogger("prosic.occupancy")
LIVE_KEY = "prosic:ocupacion-viva"
TREND_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS occ_reading (
    day TEXT NOT NULL, slot TEXT NOT NULL, ts TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (day, slot));
"""


class ReadingStore:
    """Lecturas guardadas (corte de la mañana y última lectura en vivo), en la base local del BI."""

    def __init__(self, path: str):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def save(self, day: str, slot: str, reading: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO occ_reading VALUES (?, ?, ?, ?) ON CONFLICT(day, slot) DO UPDATE SET "
                               "ts = excluded.ts, doc = excluded.doc", (day, slot, reading["ts"], json.dumps(reading)))

    def get(self, day: str, slot: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT doc FROM occ_reading WHERE day = ? AND slot = ?", (day, slot)).fetchone()
        return json.loads(r[0]) if r else None

    def latest(self, slot: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT doc FROM occ_reading WHERE slot = ? ORDER BY day DESC, ts DESC LIMIT 1",
                                   (slot,)).fetchone()
        return json.loads(r[0]) if r else None

    def clear(self) -> None:  # pruebas
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM occ_reading")


@lru_cache(maxsize=4)
def get_reading_store(path: str) -> ReadingStore:
    return ReadingStore(path)


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.ODOO_TZ))


def live_ttl(settings: Settings) -> int:
    return max(5, settings.OCCUPANCY_LIVE_MINUTES) * 60


def _m2o(v: Any) -> int | None:
    return v[0] if isinstance(v, (list, tuple)) and v else (v if isinstance(v, int) else None)


# ------------------------------------------------------------------ lectura de Odoo
async def read_odoo(odoo: OdooClient, settings: Settings) -> dict[str, Any]:
    """Estado y almacén actuales de los equipos de la flota de renta (los del histórico local).

    Devuelve ``{"ts", "units": [[vehículo, estado, almacén], ...]}``: crudo, para filtrar después por almacén."""
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    meta = await asyncio.to_thread(store.meta)
    if not meta.get("built_until"):
        raise RuntimeError("El histórico de flota aún no se genera")
    base = await asyncio.to_thread(store.units_on, date.fromisoformat(meta["built_until"]), None)
    known_wh = {u["id"]: u["warehouse_id"] for u in base}
    ids = sorted(known_wh)
    rows: list[dict[str, Any]] = []
    for i in range(0, len(ids), 2000):
        rows += await odoo.search_read("fleet.vehicle", [("id", "in", ids[i:i + 2000])],
                                       ["state_id", "current_warehouse_id"], limit=2000)
    units = [[r["id"], _m2o(r.get("state_id")), _m2o(r.get("current_warehouse_id")) or known_wh.get(r["id"])]
             for r in rows]  # archivados en Odoo desde la última sincronización ya no aparecen: salen de la flota
    return {"ts": _now(settings).isoformat(timespec="seconds"), "units": units}


async def live_reading(odoo: OdooClient, cache: Cache, settings: Settings) -> dict[str, Any] | None:
    """Lectura del momento (caché compartido); si Odoo falla, la última guardada."""
    hit = await cache.get_json(LIVE_KEY)
    if hit:
        return hit
    readings = get_reading_store(settings.HISTORY_DB_PATH)
    try:
        r = await read_odoo(odoo, settings)
    except Exception as exc:  # noqa: BLE001 — Odoo caído o en pausa: se usa lo último que se leyó
        log.warning("Lectura de ocupación en vivo falló: %s", exc)
        last = await asyncio.to_thread(readings.latest, "viva")
        return {**last, "stale": True} if last else None
    await cache.set_json(LIVE_KEY, r, live_ttl(settings))
    await asyncio.to_thread(readings.save, r["ts"][:10], "viva", r)
    return r


async def take_morning_reading(odoo: OdooClient, cache: Cache, settings: Settings,
                               now: datetime | None = None) -> bool:
    """Corte de la mañana (una vez al día, desde OCCUPANCY_MORNING_TIME). True si se tomó."""
    if not settings.OCCUPANCY_MORNING_TIME:
        return False
    now = now or _now(settings)
    try:
        hh, mm = (int(x) for x in settings.OCCUPANCY_MORNING_TIME.split(":")[:2])
    except ValueError:
        return False
    if (now.hour, now.minute) < (hh, mm) or now.hour >= 12:
        return False  # pasado el mediodía ya no es «de la mañana»: se omite ese día
    readings = get_reading_store(settings.HISTORY_DB_PATH)
    if await asyncio.to_thread(readings.get, now.date().isoformat(), "manana"):
        return False
    r = {**await read_odoo(odoo, settings), "ts": now.isoformat(timespec="seconds")}
    await asyncio.to_thread(readings.save, now.date().isoformat(), "manana", r)
    await asyncio.to_thread(readings.save, now.date().isoformat(), "viva", r)
    await cache.set_json(LIVE_KEY, r, live_ttl(settings))
    log.info("Corte de ocupación de la mañana tomado (%d equipos)", len(r["units"]))
    return True


# ------------------------------------------------------------------ armado de la respuesta
def _summary(units: list[list[int | None]], occ: set[int], excl: set[int], wh: set[int] | None,
             states: dict[int, str], wh_names: dict[int, str]) -> dict[str, Any]:
    fleet = [u for u in units if u[1] not in excl and (wh is None or u[2] in wh)]
    busy = [u for u in fleet if u[1] in occ]
    groups = Counter(STATE_GROUPS.get(states.get(u[1] or 0, "").lower(), "Otros") for u in fleet)
    by_wh: dict[int | None, list[int]] = defaultdict(lambda: [0, 0])
    for u in fleet:
        by_wh[u[2]][0] += 1
        by_wh[u[2]][1] += u[1] in occ
    return {
        "flota": len(fleet), "ocupadas": len(busy), "pct": round(len(busy) / len(fleet) * 100, 1) if fleet else None,
        "estados": [{"grupo": g, "unidades": n} for g, n in groups.most_common()],
        "almacenes": sorted(({"id": w, "nombre": wh_names.get(w or 0, "Sin almacén"), "flota": f, "ocupadas": o,
                              "pct": round(o / f * 100, 1) if f else None} for w, (f, o) in by_wh.items()),
                            key=lambda x: -x["flota"]),
    }


def _reading(r: dict[str, Any] | None, *args: Any) -> dict[str, Any] | None:
    if not r:
        return None
    return {"ts": r["ts"], "stale": bool(r.get("stale")), **_summary(r["units"], *args)}


def _close_series(store: Any, d_from: date, d_to: date, wh: list[int] | None, occ: set[int],
                  excl: set[int]) -> dict[str, list[int]]:
    days: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for day, _, st, n in store.counts(d_from, d_to, wh):
        if st in excl:
            continue
        days[day][0] += n
        days[day][1] += n if st in occ else 0
    return days


async def build(odoo: OdooClient, cache: Cache, settings: Settings, wh: list[int] | None) -> dict[str, Any]:
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    meta = await asyncio.to_thread(store.meta)
    if not meta.get("built_until"):
        raise RuntimeError("El histórico de flota aún no se genera: se crea con la primera sincronización con Odoo.")
    states = dict(await asyncio.to_thread(store.states))
    occ, excl = resolve_states(list(states.items()), settings)
    wh_names = await asyncio.to_thread(store.warehouses)
    wh_set = set(wh) if wh else None
    args = (occ, excl, wh_set, states, wh_names)
    now = _now(settings)
    today = now.date()

    # último cierre: hoy si ya pasó la corrida nocturna (23:00), si no, ayer
    built = date.fromisoformat(meta["built_until"])
    last_sync = datetime.fromisoformat(meta["last_sync"]) if meta.get("last_sync") else None
    closed_today = built >= today and last_sync is not None and last_sync.hour >= settings.FLEET_DAY_CLOSE_HOUR \
        and last_sync.date() == today
    close_day = today if closed_today else min(built, today - timedelta(days=1))
    series = await asyncio.to_thread(_close_series, store, close_day - timedelta(days=TREND_DAYS - 1), close_day,
                                     wh, occ, excl)
    trend = [{"dia": d, "flota": f, "ocupadas": o, "pct": round(o / f * 100, 1) if f else None}
             for d, (f, o) in sorted(series.items())]
    close = next((t for t in reversed(trend) if t["dia"] == close_day.isoformat()), None)
    if close:
        units = await asyncio.to_thread(store.units_on, close_day, wh)
        close = {**close, "ts": f"{close_day.isoformat()}T{settings.FLEET_DAY_CLOSE_HOUR % 24:02d}:00:00",
                 **{k: v for k, v in _summary([[u["id"], u["state_id"], u["warehouse_id"]] for u in units], *args).items()
                    if k in ("estados", "almacenes")}}
    month = [t["pct"] for t in trend if t["dia"][:7] == close_day.isoformat()[:7] and t["pct"] is not None]

    readings = get_reading_store(settings.HISTORY_DB_PATH)
    morning = await asyncio.to_thread(readings.get, today.isoformat(), "manana")
    live = await live_reading(odoo, cache, settings)
    return {
        "ahora": _reading(live, *args),
        "manana": _reading(morning, *args),
        "cierre": close,
        "tendencia": trend,
        "promedio_mes": round(sum(month) / len(month), 1) if month else None,
        "meta": settings.OCCUPANCY_GOAL_PCT,
        "minimo": settings.ALERT_UTILIZATION_MIN_PCT,
        "estados_ocupados": [state_label(states[s]) for s in sorted(occ) if s in states],
        "estados_excluidos": [state_label(states[s]) for s in sorted(excl) if s in states],
        "hora_manana": settings.OCCUPANCY_MORNING_TIME,
        "hora_cierre": f"{settings.FLEET_DAY_CLOSE_HOUR % 24:02d}:00",
        "minutos_vivo": settings.OCCUPANCY_LIVE_MINUTES,
    }
