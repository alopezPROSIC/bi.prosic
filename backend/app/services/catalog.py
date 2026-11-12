"""Catálogos para filtros: compañías, almacenes y sucursales."""
from __future__ import annotations

import unicodedata

from ..cache import Cache, make_key
from ..odoo_client import OdooClient, OdooModelUnavailable
from ..odoo_mapping import BRANCH_MODEL, COMPANY, WAREHOUSE
from ..schemas.odoo import FilterOption, FilterOptions
from .common import m2o_id


async def filter_options(odoo: OdooClient, cache: Cache, ttl: int = 3600) -> FilterOptions:
    key = make_key("catalog", "filters")
    hit = await cache.get_json(key)
    if hit:
        return FilterOptions.model_validate(hit)
    companies = await odoo.search_read(COMPANY.model, [], ["id", "name"], order="sequence, name")
    warehouses = await odoo.search_read(WAREHOUSE.model, [], ["id", "name", "company_id"], order="sequence, name")
    branches: list[dict] = []
    if BRANCH_MODEL:
        try:
            branches = await odoo.search_read(BRANCH_MODEL, [], ["id", "name"], order="name")
        except OdooModelUnavailable:
            branches = []
    result = FilterOptions(
        companies=[FilterOption(id=c["id"], name=c["name"]) for c in companies],
        warehouses=[FilterOption(id=w["id"], name=w["name"], company_id=m2o_id(w.get("company_id"))) for w in warehouses],
        branches=[FilterOption(id=b["id"], name=b["name"]) for b in branches],
    )
    await cache.set_json(key, result.model_dump(mode="json"), ttl)
    return result


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def resolve(options: list[FilterOption], name: str | None) -> tuple[int | None, str | None]:
    """Resuelve un nombre libre a un id. Devuelve (id, error)."""
    if not name:
        return None, None
    n = _norm(name)
    exact = [o for o in options if _norm(o.name) == n]
    partial = exact or [o for o in options if n in _norm(o.name) or _norm(o.name) in n]
    if len(partial) == 1:
        return partial[0].id, None
    names = ", ".join(o.name for o in options) or "(ninguno)"
    if not partial:
        return None, f"No encontré «{name}». Opciones válidas: {names}"
    return None, f"«{name}» es ambiguo. Coincide con: {', '.join(o.name for o in partial)}"
