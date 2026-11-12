from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class OdooHealth(BaseModel):
    ok: bool
    server_version: str | None = None
    server_serie: str | None = None
    database: str
    user: dict[str, Any] | None = None
    companies: list[dict[str, Any]] = []
    redis: bool = False
    ai_enabled: bool = False
    error: str | None = None


class FilterOption(BaseModel):
    id: int
    name: str
    company_id: int | None = None


class FilterOptions(BaseModel):
    companies: list[FilterOption]
    warehouses: list[FilterOption]
    branches: list[FilterOption]
    odoo_url: str | None = None
    # Alcance del usuario (almacén/sucursal fijos); el frontend bloquea esos filtros.
    scope: dict[str, Any] = {}
