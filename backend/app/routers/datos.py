"""Datos de Odoo: consultas a la medida (extractor a Excel) y qué se sincroniza cada noche."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import User, current_user, require_admin
from ..cache import Cache
from ..config import Settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError, OdooModelUnavailable
from ..schemas.kpis import KpiFilters
from ..services import data_explorer as dx
from ..services import fleet_history
from ..services.registry import next_sync
from ..services.scheduler import refresh_module
from ..store import get_store

router = APIRouter(prefix="/api/datos", tags=["datos"])


def _need(user: User) -> None:
    if not user.can("datos"):
        raise HTTPException(403, "Tu rol no tiene acceso a «Datos de Odoo»")


def _store(settings: Settings) -> dx.QueryStore:
    return dx.get_query_store(settings.HISTORY_DB_PATH)


def _scope(user: User) -> KpiFilters:
    today = date.today()
    return user.scope_filters(KpiFilters(date_from=today, date_to=today, compare="none"))


async def _meta(odoo: OdooClient, source: str) -> dict[str, dict[str, Any]]:
    src = dx.SOURCES.get(source)
    if not src:
        raise HTTPException(404, "Fuente desconocida")
    try:
        return await odoo.fields_get(src.model, lang=runtime_settings().DATA_LANG or None)
    except OdooModelUnavailable as exc:
        raise HTTPException(404, f"«{src.label}» no está disponible en esta instancia de Odoo: {exc.reason}") from exc
    except OdooError as exc:
        raise HTTPException(502, f"Odoo no disponible: {exc}") from exc


# ------------------------------------------------------------------ fuentes y campos
@router.get("/fuentes")
async def sources(user: User = Depends(current_user)) -> dict[str, Any]:
    _need(user)
    scoped = user.scoped
    out = []
    for s in dx.SOURCES.values():
        blocked = scoped and ((user.branch_ids and not s.branch_field) or (user.warehouse_ids and not s.warehouse_field))
        out.append({"key": s.key, "label": s.label, "descripcion": s.description, "modelo": s.model,
                    "fechas": s.date_fields, "columnas": s.columns, "fecha_opcional": s.date_optional,
                    "disponible": not blocked,
                    "motivo": "No se puede filtrar por tu sucursal/almacén" if blocked else None})
    return {"fuentes": out, "tope": runtime_settings().DATA_EXPORT_MAX_ROWS}


@router.get("/fuentes/{source}/campos")
async def source_fields(source: str = Path(max_length=40), user: User = Depends(current_user),
                        odoo: OdooClient = Depends(get_odoo)) -> dict[str, Any]:
    _need(user)
    return {"fuente": source, "campos": dx.field_list(await _meta(odoo, source), dx.SOURCES[source].delegated)}


# ------------------------------------------------------------------ consultas guardadas
@router.get("/consultas")
async def list_queries(user: User = Depends(current_user), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    _need(user)
    rows = _store(settings).visible(user.username)
    for r in rows:
        r["editable"] = user.is_admin or r["propietario"] in (user.username, dx.SYSTEM_OWNER)
    return {"consultas": rows}


@router.put("/consultas")
async def save_query(body: dx.Consulta, user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                     settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    _need(user)
    try:
        dx.validate(body, await _meta(odoo, body.fuente))
        saved = _store(settings).save(body, user.username, user.is_admin)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    get_store(settings.HISTORY_DB_PATH).log(user.username, "consulta_guardada", None, f"{saved['nombre']} ({body.fuente})")
    return {**saved, "editable": True}


@router.delete("/consultas/{qid}")
async def delete_query(qid: str = Path(max_length=40), user: User = Depends(current_user),
                       settings: Settings = Depends(runtime_settings)) -> dict[str, bool]:
    _need(user)
    try:
        if not _store(settings).delete(qid, user.username, user.is_admin):
            raise HTTPException(404, "La consulta no existe")
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"ok": True}


# ------------------------------------------------------------------ ejecutar / Excel
class RunIn(BaseModel):
    consulta: dx.Consulta
    vista_previa: bool = True
    actualizar: bool = False
    desde: date | None = None
    hasta: date | None = None


async def _run(body: RunIn, user: User, odoo: OdooClient, cache: Cache, settings: Settings,
               preview: bool) -> dict[str, Any]:
    _need(user)
    await _meta(odoo, body.consulta.fuente)
    try:
        return await dx.run(odoo, cache, settings, body.consulta, _scope(user), preview=preview,
                            refresh=body.actualizar, desde=body.desde, hasta=body.hasta)
    except dx.TooManyRows as exc:
        raise HTTPException(413, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"Odoo no pudo responder la consulta: {exc}") from exc


@router.post("/ejecutar")
async def run_query(body: RunIn, user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                    cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    return await _run(body, user, odoo, cache, settings, body.vista_previa)


@router.post("/excel")
async def excel(body: RunIn, user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> Response:
    result = await _run(body, user, odoo, cache, settings, preview=False)
    q = body.consulta
    meta = await _meta(odoo, q.fuente)
    today = datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
    params = dx.describe(q, meta, dx.period_range(q.periodo, today, body.desde, body.hasta))
    data = await asyncio.to_thread(dx.build_xlsx, q, result, params, settings, user.name or user.username)
    get_store(settings.HISTORY_DB_PATH).log(user.username, "excel_datos", None, f"{q.nombre}: {result['total']} registros")
    name = f"{q.nombre} {today.isoformat()}.xlsx"
    ascii_name = "".join(c if c.isascii() and (c.isalnum() or c in " -_.") else "_" for c in name)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})


# ------------------------------------------------------------------ sincronización (administradores)
class SyncIn(BaseModel):
    modulos: list[str]


def _sync_view(settings: Settings) -> dict[str, Any]:
    status = dx.sync_status(settings)
    enabled = set(dx.nightly_modules(settings))
    nxt = next_sync(settings)
    store = fleet_history.get_fleet_store(settings.HISTORY_DB_PATH)
    meta = store.meta() if store.enabled else {}
    return {
        "hora": settings.NIGHTLY_SYNC_TIME or None,
        "siguiente": nxt.isoformat() if nxt else None,
        "modulos": [{"key": k, "label": v, "activo": k in enabled, "ultimo": status.get(k)} for k, v in dx.SYNC_MODULES.items()],
        "flota": {"ultimo": status.get("flota"), "ultima_sincronizacion": meta.get("last_sync")},
        "hora_manana": settings.OCCUPANCY_MORNING_TIME or None,
        "tope_llamadas_min": settings.ODOO_MAX_CALLS_PER_MINUTE,
        "tope_registros": settings.DATA_EXPORT_MAX_ROWS,
    }


@router.get("/sincronizacion")
async def sync_view(_: User = Depends(require_admin), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    return await asyncio.to_thread(_sync_view, settings)


@router.put("/sincronizacion")
async def sync_save(body: SyncIn, admin: User = Depends(require_admin)) -> dict[str, Any]:
    unknown = set(body.modulos) - set(dx.SYNC_MODULES)
    if unknown:
        raise HTTPException(400, f"Módulos desconocidos: {', '.join(sorted(unknown))}")
    value = ",".join(m for m in dx.SYNC_MODULES if m in body.modulos)
    settings = runtime_settings()
    get_store(settings.HISTORY_DB_PATH).update_settings({"NIGHTLY_MODULES": value})
    get_store(settings.HISTORY_DB_PATH).log(admin.username, "sincronizacion_actualizada", None, value or "(ninguno)")
    return await asyncio.to_thread(_sync_view, runtime_settings())


_manual: dict[str, float] = {}


@router.post("/sincronizacion/{module}")
async def sync_now(module: str = Path(max_length=20), admin: User = Depends(require_admin),
                   odoo: OdooClient = Depends(get_odoo), cache: Cache = Depends(get_cache)) -> dict[str, Any]:
    if module not in dx.SYNC_MODULES:
        raise HTTPException(404, "Módulo desconocido")
    loop = asyncio.get_running_loop()
    if loop.time() - _manual.get(module, -1e9) < dx.REFRESH_EVERY:
        raise HTTPException(429, "Ese módulo se trajo de Odoo hace menos de 10 minutos; espera un momento.")
    _manual[module] = loop.time()
    settings = runtime_settings()
    try:
        await refresh_module(odoo, cache, settings, module)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo traer de Odoo: {exc}") from exc
    get_store(settings.HISTORY_DB_PATH).log(admin.username, "sincronizacion_manual", None, module)
    return await asyncio.to_thread(_sync_view, settings)
