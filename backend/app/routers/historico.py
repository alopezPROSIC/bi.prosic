"""Histórico de flota: consulta, filtros y descarga de los registros por equipo y día; sincronización (admin).

Dos estructuras (las mismas columnas que usa PROSIC en sus reportes):

- ``diario``: un renglón por equipo y día, con disponibilidad, días en el estado, subasta y si estaba rentado.
- ``corte``: un renglón por equipo al cierre de cada mes del periodo (o al último día elegido).

Todo se lee de las tablas propias del sistema (``fh_*``), no de Odoo.
"""
from __future__ import annotations

import asyncio
import csv
import io
from calendar import monthrange
from datetime import date
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..auth import User, current_user, require_admin, require_module
from ..config import Settings, runtime_settings
from ..deps import get_odoo
from ..odoo_client import OdooClient, OdooError
from ..schemas.kpis import KpiFilters
from ..services import fleet_history
from ..services.fleet_history import MAX_EXPORT_ROWS, FleetStore, get_fleet_store, state_label
from ..services.historico import resolve_states

router = APIRouter(prefix="/api/historico", tags=["historico"])

Formato = Literal["diario", "corte"]

# Estado agrupado (Estado_D): junta las variantes de un mismo estado.
STATE_GROUPS = {"available": "Disponible", "rented": "Rentado", "rented - service": "Rentado",
                "to deliver": "Por entregar", "inspection": "Mantenimiento", "service - garage": "Mantenimiento",
                "to collect": "Por recolectar", "subasta": "Subasta"}

# (llave, encabezado, tipo). Los encabezados son los de los reportes de PROSIC.
COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "diario": [
        ("nombre", "name", "text"), ("lote", "Lote/Nº de serie", "text"), ("producto", "[Producto/Nombre]", "text"),
        ("familia", "Familia", "text"), ("subfamilia", "SubFamilia", "text"),
        ("almacen", "Almacen/Nombre del almacén", "text"), ("estado", "Estado", "text"), ("fecha", "Fecha", "date"),
        ("disponibilidad", "Disponibilidad", "text"), ("centro", "CentrDeCosto", "text"),
        ("propietario", "Propietario", "text"), ("centro2", "CentrDeCosto2", "text"), ("dia", "Día", "number"),
        ("date_duration", "date_duration", "date"), ("duration_day", "duration_day", "number"),
        ("estado_d", "Estado_D", "text"), ("subasta", "subasta", "text"), ("rentado", "Rentado", "text"),
    ],
    "corte": [
        ("nombre", "name", "text"), ("lote", "Lote/Nº de serie", "text"), ("producto", "[Producto/Nombre]", "text"),
        ("subfamilia", "SubFamilia", "text"), ("almacen", "Almacen/Nombre del almacén", "text"),
        ("estado", "Estado", "text"), ("fecha", "Fecha", "date"), ("centro", "CentrDeCosto", "text"),
        ("propietario", "Propietario", "text"), ("date_duration", "date_duration", "date"),
        ("duration_day", "duration_day", "number"),
    ],
}


def cut_days(d_from: date, d_to: date) -> list[date]:
    """Último día de cada mes del periodo; el último corte es ``d_to`` si el mes no ha terminado."""
    out: list[date] = []
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        out.append(min(date(y, m, monthrange(y, m)[1]), d_to))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def shape(r: dict[str, Any], fmt: str, occupied: set[int]) -> dict[str, Any]:
    """Registro de ``FleetStore.records`` → renglón con las columnas de la estructura elegida."""
    day = date.fromisoformat(r["day"])
    since = date.fromisoformat(r["since"]) if r.get("since") else None
    key = (r["state"] or "").strip().lower()
    nombre = (r["plate"] or r["serie"]) if fmt == "diario" else (r["serial_no"] or r["name"])
    return {
        "nombre": nombre, "lote": r["serie"], "producto": r["product"], "familia": r["familia"],
        "subfamilia": r["subfamilia"], "almacen": r["warehouse"], "estado": state_label(r["state"]),
        "fecha": r["day"], "disponibilidad": "Disponible" if key == "available" else "No disponible",
        "centro": r["cost_center"], "propietario": r["owner"], "centro2": r["cost_center"], "dia": day.day,
        "date_duration": since.isoformat() if since else None,
        "duration_day": (day - since).days if since else None,
        "estado_d": STATE_GROUPS.get(key, state_label(r["state"])),
        "subasta": "Sí" if r["subasta"] or key == "subasta" else "No",
        "rentado": "Rentado" if r["state_id"] in occupied else "No Rentado",
    }


def _dmy(iso: Any) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if isinstance(iso, str) and len(iso) >= 10 else ""


def _csv(rows: list[dict[str, Any]], fmt: str, occupied: set[int]) -> Iterator[str]:
    cols = COLUMNS[fmt]
    buf = io.StringIO()
    w = csv.writer(buf)
    buf.write("﻿")  # BOM: Excel abre los acentos correctamente
    w.writerow([label for _, label, _ in cols])
    for i, r in enumerate(rows, 1):
        row = shape(r, fmt, occupied)
        w.writerow([_dmy(row[k]) if t == "date" else ("" if row[k] is None else row[k]) for k, _, t in cols])
        if i % 5000 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


class Query_:
    """Parámetros comunes de consulta y descarga."""

    def __init__(self, desde: date | None = None, hasta: date | None = None, formato: Formato = "diario",
                 warehouse_id: int | None = None, estado: int | None = None, familia: str | None = None,
                 subfamilia: str | None = None, centro: str | None = None, propietario: str | None = None,
                 subasta: Literal["si", "no"] | None = None, q: str | None = Query(None, max_length=80)):
        self.desde, self.hasta, self.formato, self.warehouse_id = desde, hasta, formato, warehouse_id
        self.flt: dict[str, Any] = {"state_id": estado, "familia": familia, "subfamilia": subfamilia,
                                    "cost_center": centro, "owner": propietario, "q": q,
                                    "subasta": None if subasta is None else subasta == "si"}


async def _prepare(p: Query_, user: User, settings: Settings) -> tuple[FleetStore, dict[str, Any]]:
    require_module(user, "historico")
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    if not store.enabled:
        raise HTTPException(503, "Histórico de flota no disponible")
    meta = await asyncio.to_thread(store.meta)
    first, last = await asyncio.to_thread(store.first_day), meta.get("built_until")
    if not first or not last:
        raise HTTPException(409, "El histórico aún no se genera: se crea con la primera sincronización con Odoo.")
    d_from = max(p.desde or date.fromisoformat(first), date.fromisoformat(first))
    d_to = min(p.hasta or date.fromisoformat(last), date.fromisoformat(last))
    if d_to < d_from:
        raise HTTPException(422, "La fecha final debe ser posterior a la inicial")
    # Alcance del usuario (mismo criterio que el módulo): sin sucursal en la flota, solo almacenes.
    scoped = user.scope_filters(KpiFilters(date_from=d_to, date_to=d_to, compare="none", warehouse_id=p.warehouse_id))
    if scoped.strict_scope and (scoped.branch_id or scoped.allowed_branch_ids):
        raise HTTPException(403, "El histórico de flota no se puede filtrar por sucursal; pide acceso por almacén.")
    wh = [scoped.warehouse_id] if scoped.warehouse_id else list(scoped.allowed_warehouse_ids or [])
    occupied, _ = resolve_states(await asyncio.to_thread(store.states), settings)
    return store, {"d_from": d_from, "d_to": d_to, "wh": wh or None, "occupied": occupied, "first": first,
                   "last": last, "days": cut_days(d_from, d_to) if p.formato == "corte" else None}


@router.get("/registros")
async def records(p: Query_ = Depends(), page: int = Query(1, ge=1), page_size: int = Query(50, ge=10, le=200),
                  user: User = Depends(current_user), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    """Registros del histórico, paginados, con los valores disponibles para cada filtro."""
    store, c = await _prepare(p, user, settings)
    args = (c["d_from"], c["d_to"], c["wh"], p.flt, c["days"])
    total = await asyncio.to_thread(store.count_records, *args)
    rows = await asyncio.to_thread(store.records, *args, limit=page_size, offset=(page - 1) * page_size,
                                   newest_first=True)
    opts = await asyncio.to_thread(store.options)
    states = await asyncio.to_thread(store.states)
    return {
        "formato": p.formato, "desde": c["d_from"].isoformat(), "hasta": c["d_to"].isoformat(),
        "first_day": c["first"], "last_day": c["last"], "total": total, "page": page, "page_size": page_size,
        "max_export_rows": MAX_EXPORT_ROWS,
        "columns": [{"key": k, "label": label, "type": t, "entity": None} for k, label, t in COLUMNS[p.formato]],
        "rows": [shape(r, p.formato, c["occupied"]) for r in rows],
        "options": {"estados": [{"id": i, "label": state_label(n)} for i, n in states], **opts},
    }


@router.get("/flota.csv")
async def export_csv(p: Query_ = Depends(), user: User = Depends(current_user),
                     settings: Settings = Depends(runtime_settings)) -> StreamingResponse:
    """Descarga los registros filtrados (CSV para Excel, fechas dd/mm/aaaa)."""
    store, c = await _prepare(p, user, settings)
    args = (c["d_from"], c["d_to"], c["wh"], p.flt, c["days"])
    total = await asyncio.to_thread(store.count_records, *args)
    if total > MAX_EXPORT_ROWS:
        raise HTTPException(422, f"Son {total:,} renglones y Excel abre hasta {MAX_EXPORT_ROWS:,}. Acorta el periodo, "
                                 "agrega un filtro o usa la estructura «Corte mensual».")
    rows = await asyncio.to_thread(store.records, *args)
    name = f"historico_flota_{p.formato}_{c['d_from']:%Y%m%d}_{c['d_to']:%Y%m%d}.csv"
    return StreamingResponse(_csv(rows, p.formato, c["occupied"]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "no-store"})


@router.get("/estado")
async def status(user: User = Depends(current_user), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    require_module(user, "historico")
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    if not store.enabled:
        return {"enabled": False}
    meta = await asyncio.to_thread(store.meta)
    return {"enabled": True, "last_sync": meta.get("last_sync"), "first_day": await asyncio.to_thread(store.first_day),
            "last_day": meta.get("built_until"), "max_export_rows": MAX_EXPORT_ROWS}


@router.post("/sincronizar")
async def sync_now(_: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                   settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    try:
        return await fleet_history.sync(odoo, settings)
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
