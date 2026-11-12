from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from ..auth import User, current_user, require_module
from ..cache import Cache
from ..config import Settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError
from ..schemas.kpis import CompareMode, KpiFilters, ModuleKpis, ModuleName
from ..services.registry import get_module_kpis

router = APIRouter(prefix="/api/kpis", tags=["kpis"])


def parse_filters(
    date_from: date = Query(...),
    date_to: date = Query(...),
    company_id: int | None = Query(None, ge=1),
    warehouse_id: int | None = Query(None, ge=1),
    branch_id: int | None = Query(None, ge=1),
    compare: CompareMode = Query("previous"),
) -> KpiFilters:
    try:
        return KpiFilters(date_from=date_from, date_to=date_to, company_id=company_id,
                          warehouse_id=warehouse_id, branch_id=branch_id, compare=compare)
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from exc


@router.get("/{modulo}", response_model=ModuleKpis)
async def kpis(modulo: ModuleName, filters: KpiFilters = Depends(parse_filters), refresh: bool = False,
               user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
               cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> ModuleKpis:
    require_module(user, modulo)
    try:
        return await get_module_kpis(odoo, cache, settings, modulo, user.scope_filters(filters), use_cache=not refresh)
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
