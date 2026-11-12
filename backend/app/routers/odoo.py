from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path

from ..auth import User, current_user, require_admin
from ..cache import Cache
from ..config import Settings, get_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError, OdooModelUnavailable
from ..schemas.odoo import FilterOptions, OdooHealth
from ..services.catalog import filter_options

router = APIRouter(prefix="/api/odoo", tags=["odoo"])
_MODEL_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


@router.get("/health", response_model=OdooHealth)
async def health(_: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                 cache: Cache = Depends(get_cache), settings: Settings = Depends(get_settings)) -> OdooHealth:
    out = OdooHealth(ok=False, database=settings.ODOO_DB, redis=await cache.ping(), ai_enabled=settings.ai_enabled)
    try:
        ver = await odoo.version()
        out.server_version = ver.get("server_version")
        serie = ver.get("server_serie")
        out.server_serie = serie
        uid = await odoo.uid()
        users = await odoo.search_read("res.users", [("id", "=", uid)], ["name", "login", "company_id", "company_ids"])
        if users:
            u = users[0]
            out.user = {"id": uid, "name": u["name"], "login": u["login"],
                        "company_id": u["company_id"][0] if u.get("company_id") else None}
            ids = u.get("company_ids") or []
            out.companies = await odoo.search_read("res.company", [("id", "in", ids)], ["id", "name", "currency_id"])
        out.ok = True
    except OdooError as exc:
        out.error = str(exc)
    return out


@router.get("/filters", response_model=FilterOptions)
async def filters(user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                  cache: Cache = Depends(get_cache), settings: Settings = Depends(get_settings)) -> FilterOptions:
    try:
        opts = await filter_options(odoo, cache)
    except OdooError as exc:
        raise HTTPException(502, f"Odoo no disponible: {exc}") from exc
    # La URL de Odoo no es una credencial: el frontend la usa para enlazar registros (drill-down).
    opts = opts.model_copy(update={"odoo_url": settings.ODOO_URL})
    if user.warehouse_ids:
        opts.warehouses = [w for w in opts.warehouses if w.id in user.warehouse_ids]
    if user.branch_ids:
        opts.branches = [b for b in opts.branches if b.id in user.branch_ids]
    # Con un solo almacén/sucursal el filtro queda fijo; con varios se puede elegir entre ellos.
    opts.scope = {"warehouse_id": user.warehouse_id, "branch_id": user.branch_id,
                  "warehouse_ids": list(user.warehouse_ids), "branch_ids": list(user.branch_ids)}
    return opts


@router.get("/fields/{model}")
async def fields(model: str = Path(max_length=128), _: User = Depends(require_admin),
                 odoo: OdooClient = Depends(get_odoo)) -> dict[str, Any]:
    """Descubre los campos de un modelo (incluye personalizados x_ / x_studio_)."""
    if not _MODEL_RE.match(model):
        raise HTTPException(400, "Nombre de modelo inválido")
    try:
        data = await odoo.fields_get(model)
    except OdooModelUnavailable as exc:
        raise HTTPException(404, exc.reason) from exc
    except OdooError as exc:
        raise HTTPException(502, str(exc)) from exc
    fields_sorted = dict(sorted(data.items()))
    return {
        "model": model,
        "count": len(fields_sorted),
        "custom": [k for k in fields_sorted if k.startswith("x_")],
        "fields": fields_sorted,
    }


@router.post("/cache/clear")
async def clear_cache(_: User = Depends(require_admin), cache: Cache = Depends(get_cache)) -> dict[str, int]:
    n = 0
    for prefix in ("kpi", "ai", "catalog"):
        n += await cache.delete_prefix(prefix)
    return {"deleted": n}
