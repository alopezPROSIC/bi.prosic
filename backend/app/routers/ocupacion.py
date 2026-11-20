"""Tacómetro de ocupación diaria de la flota (mañana, ahora y cierre de la noche).

Lo ve quien tenga acceso a «Rentas e inventario» o al «Histórico de flota», limitado a sus almacenes.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import User, current_user
from ..cache import Cache
from ..config import Settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiFilters
from ..services import occupancy

router = APIRouter(prefix="/api/ocupacion", tags=["ocupacion"])


@router.get("")
async def daily_occupancy(warehouse_id: int | None = Query(None, ge=1), user: User = Depends(current_user),
                          odoo: OdooClient = Depends(get_odoo), cache: Cache = Depends(get_cache),
                          settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    if not (user.can("rentas") or user.can("historico")):
        raise HTTPException(403, "Tu rol no tiene acceso a la flota")
    today = date.today()
    scoped = user.scope_filters(KpiFilters(date_from=today, date_to=today, compare="none", warehouse_id=warehouse_id))
    if scoped.strict_scope and (scoped.branch_id or scoped.allowed_branch_ids) and not (
            scoped.warehouse_id or scoped.allowed_warehouse_ids):
        raise HTTPException(403, "La ocupación de flota se filtra por almacén; pide acceso por almacén.")
    wh = [scoped.warehouse_id] if scoped.warehouse_id else list(scoped.allowed_warehouse_ids or [])
    try:
        return await occupancy.build(odoo, cache, settings, wh or None)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
