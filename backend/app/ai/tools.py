"""Herramientas (function calling) expuestas al modelo.

El modelo SOLO puede invocar estas funciones predefinidas con parámetros
validados. Nunca genera dominios de Odoo ni consultas arbitrarias: cada
herramienta se traduce a un cálculo de KPIs del backend.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from pydantic import BaseModel, ValidationError, field_validator

from ..cache import Cache
from ..config import Settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiFilters, ModuleKpis
from ..services.catalog import filter_options, resolve
from ..services.registry import get_module_kpis
from .anonymizer import Anonymizer, ai_payload

_NULLABLE_STR = {"type": ["string", "null"]}
_DATE = {"type": "string", "description": "Fecha ISO AAAA-MM-DD"}
_COMMON = {
    "compania": {**_NULLABLE_STR, "description": "Nombre de la compañía; null = todas"},
}


def _fn(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "additionalProperties": False, "required": list(props), "properties": props},
    }}


TOOLS: list[dict[str, Any]] = [
    _fn("get_ventas", "Ventas, ticket promedio, top clientes/vendedores/productos, facturado vs cobrado y notas de crédito.",
        {"fecha_desde": _DATE, "fecha_hasta": _DATE,
         "sucursal": {**_NULLABLE_STR, "description": "Sucursal (branch office); null = todas"},
         "almacen": {**_NULLABLE_STR, "description": "Almacén; null = todos"}, **_COMMON}),
    _fn("get_cxc", "Cuentas por cobrar con antigüedad (por vencer, 1-30, 31-60, 61-90, +90 días) y clientes con más saldo vencido.",
        {"fecha_corte": {**_NULLABLE_STR, "description": "Fecha de corte AAAA-MM-DD; null = hoy"},
         "sucursal": {**_NULLABLE_STR, "description": "Sucursal; null = todas"}, **_COMMON}),
    _fn("get_utilizacion_rentas", "Unidades disponibles/rentadas/en mantenimiento por almacén, utilización por categoría, ingresos de renta y rentas por vencer.",
        {"fecha_desde": _DATE, "fecha_hasta": _DATE,
         "almacen": {**_NULLABLE_STR, "description": "Almacén; null = todos"}, **_COMMON}),
    _fn("get_historico_flota",
        "Histórico diario de la flota de renta (desde dic-2022): % de ocupación promedio del periodo, promedio diario "
        "de unidades por estado (disponible, por entregar, rentado, rentado-servicio, inspección, servicio-taller, "
        "por recolectar, subasta), promedios por almacén y por mes. Úsala para ocupación histórica, cierres de mes "
        "y comparativos entre periodos o almacenes.",
        {"fecha_desde": _DATE, "fecha_hasta": _DATE,
         "almacen": {**_NULLABLE_STR, "description": "Almacén; null = todos"}, **_COMMON}),
    _fn("get_mantenimiento", "Peticiones de mantenimiento por etapa, preventivo vs correctivo, tiempo de resolución, costo de reparación y equipos con más fallas.",
        {"fecha_desde": _DATE, "fecha_hasta": _DATE,
         "sucursal": {**_NULLABLE_STR, "description": "Sucursal; null = todas"}, **_COMMON}),
    _fn("get_compras", "Gasto por proveedor y por mes, concentración, órdenes por recibir/facturar y cuentas por pagar con antigüedad.",
        {"fecha_desde": _DATE, "fecha_hasta": _DATE,
         "sucursal": {**_NULLABLE_STR, "description": "Sucursal; null = todas"},
         "almacen": {**_NULLABLE_STR, "description": "Almacén; null = todos"}, **_COMMON}),
    _fn("get_pronostico",
        "Pronóstico a 6 meses (ventas, ingresos de renta, facturación, cobranza, compras y costo de reparación) calculado "
        "con 24 meses de historia, con rango mín–máx según el error histórico; además el calendario de vencimientos: "
        "CxC y CxP por mes de vencimiento (y lo ya vencido) y contratos de renta que vencen por mes. Úsala para "
        "pronósticos, proyecciones, flujo de efectivo esperado, presupuesto o planeación de 3 a 6 meses.",
        {"sucursal": {**_NULLABLE_STR, "description": "Sucursal; null = todas"},
         "almacen": {**_NULLABLE_STR, "description": "Almacén; null = todos"}, **_COMMON}),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def tools_without_strict() -> list[dict[str, Any]]:
    """Mismas herramientas sin ``strict`` (para proveedores compatibles como Groq, que no lo documentan).
    Los argumentos se validan igual en el backend con Pydantic antes de ejecutar nada."""
    return [{"type": "function", "function": {k: v for k, v in t["function"].items() if k != "strict"}} for t in TOOLS]


class _RangeArgs(BaseModel):
    fecha_desde: date
    fecha_hasta: date
    sucursal: str | None = None
    almacen: str | None = None
    compania: str | None = None

    @field_validator("fecha_hasta")
    @classmethod
    def _order(cls, v: date, info: Any) -> date:
        d = info.data.get("fecha_desde")
        if d and v < d:
            raise ValueError("fecha_hasta debe ser >= fecha_desde")
        if d and (v - d).days > 366 * 3:
            raise ValueError("rango máximo de 3 años")
        return v


class _CxcArgs(BaseModel):
    fecha_corte: date | None = None
    sucursal: str | None = None
    compania: str | None = None


class _ScopeArgs(BaseModel):
    sucursal: str | None = None
    almacen: str | None = None
    compania: str | None = None


# herramienta -> (módulo, modelo de argumentos, filtro de secciones para la respuesta)
_SECTION: dict[str, Callable[[str], bool]] = {
    "get_cxc": lambda key: key.startswith("cxc"),
}
_MODULE = {"get_ventas": "ventas", "get_cxc": "ventas", "get_utilizacion_rentas": "rentas",
           "get_historico_flota": "historico",
           "get_mantenimiento": "mantenimiento", "get_compras": "compras", "get_pronostico": "pronostico"}
TOOL_MODULE = _MODULE


def _subset(kpis: ModuleKpis, keep: Callable[[str], bool] | None) -> ModuleKpis:
    if not keep:
        return kpis
    return kpis.model_copy(update={
        "cards": [c for c in kpis.cards if keep(c.key)],
        "charts": [c for c in kpis.charts if keep(c.key)],
        "tables": [t for t in kpis.tables if keep(t.key)],
    })


async def run_tool(name: str, raw_args: dict[str, Any], *, odoo: OdooClient, cache: Cache, settings: Settings,
                   anon: Anonymizer, scope: Any = None) -> tuple[dict[str, Any], ModuleKpis | None]:
    """Ejecuta una herramienta. Devuelve (payload anonimizado para el modelo, KPIs reales para la UI)."""
    if name not in TOOL_NAMES:
        return {"error": f"Herramienta desconocida: {name}"}, None
    try:
        if name == "get_cxc":
            a = _CxcArgs.model_validate(raw_args)
            corte = min(a.fecha_corte or date.today(), date.today())
            d_from, d_to, sucursal, almacen = corte - timedelta(days=30), corte, a.sucursal, None
            compania = a.compania
        elif name == "get_pronostico":
            s = _ScopeArgs.model_validate(raw_args)
            d_to = date.today()
            d_from, sucursal, almacen, compania = d_to.replace(day=1), s.sucursal, s.almacen, s.compania
        else:
            r = _RangeArgs.model_validate(raw_args)
            d_from, d_to, sucursal, almacen, compania = r.fecha_desde, r.fecha_hasta, r.sucursal, r.almacen, r.compania
    except ValidationError as exc:
        return {"error": "Parámetros inválidos: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                         for e in exc.errors())}, None

    opts = await filter_options(odoo, cache)
    company_id, e1 = resolve(opts.companies, compania)
    branch_id, e2 = resolve(opts.branches, sucursal)
    warehouse_id, e3 = resolve(opts.warehouses, almacen)
    errors = [e for e in (e1, e2, e3) if e]
    if errors:
        return {"error": " | ".join(errors)}, None

    filters = KpiFilters(date_from=d_from, date_to=d_to, company_id=company_id, branch_id=branch_id,
                         warehouse_id=warehouse_id,
                         compare="none" if name in ("get_cxc", "get_pronostico") else "previous")
    if scope is not None and not scope.can(_MODULE[name]):
        return {"error": "El rol del usuario no tiene acceso a esta información."}, None
    if scope is not None and getattr(scope, "scoped", False):
        if (warehouse_id and scope.warehouse_ids and warehouse_id not in scope.warehouse_ids) or \
                (branch_id and scope.branch_ids and branch_id not in scope.branch_ids):
            return {"error": "El usuario solo tiene acceso a sus almacenes/sucursales asignados; consulta sin ese filtro."}, None
        filters = scope.scope_filters(filters)  # el usuario solo puede ver su almacén / sucursal
    kpis = await get_module_kpis(odoo, cache, settings, _MODULE[name], filters)  # type: ignore[arg-type]
    kpis = _subset(kpis, _SECTION.get(name))
    payload = ai_payload(kpis, anon)
    payload["herramienta"] = name
    payload["filtros_aplicados"] = {"compania": compania, "sucursal": sucursal, "almacen": almacen}
    return payload, kpis
