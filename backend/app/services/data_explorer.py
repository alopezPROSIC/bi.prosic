"""Extractor de datos de Odoo: tablas a la medida sobre fuentes aprobadas, con vista previa y Excel.

Finanzas (o quien tenga el permiso «datos») arma su propia tabla sin pedírsela a sistemas: elige la fuente
(facturas, líneas contables, pagos, flota…), las columnas, el periodo, los filtros (reglas) y, si quiere, la agrupa
y suma. La consulta se guarda con nombre y se puede compartir.

Cuidados con Odoo (solo lectura):
- Solo se consultan las FUENTES de la lista de abajo, con su filtro base (p. ej. solo facturas publicadas).
- Solo campos almacenados (los calculados al vuelo son caros para Odoo) y de tipos simples.
- Se cuenta antes de leer: si pasa del tope (DATA_EXPORT_MAX_ROWS) se pide acotar en lugar de traerlo todo.
- El resultado queda en caché hasta la actualización nocturna; «Actualizar desde Odoo» se permite una vez cada
  10 minutos por consulta.
- El alcance del usuario (sucursal/almacén) se aplica siempre; una fuente sin ese campo no se le muestra.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from ..cache import Cache, make_key
from ..config import Settings
from ..odoo_client import OdooClient
from ..odoo_mapping import ModelMap
from ..schemas.kpis import KpiFilters
from .common import Ctx, ScopeRestricted, scope_domain
from .registry import seconds_until_sync

FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
SIMPLE_TYPES = {"char", "text", "integer", "float", "monetary", "date", "datetime", "boolean", "selection", "many2one"}
NUMERIC = {"integer", "float", "monetary"}
GROUPABLE = {"char", "integer", "date", "datetime", "boolean", "selection", "many2one"}
GRANULARITIES = {"day": "Día", "week": "Semana", "month": "Mes", "quarter": "Trimestre", "year": "Año"}
PREVIEW_ROWS = 100
MAX_GROUPS = 5000
REFRESH_EVERY = 600  # segundos entre dos «Actualizar desde Odoo» de la misma consulta
MAX_YEARS = 3


# ------------------------------------------------------------------ fuentes aprobadas
@dataclass(frozen=True)
class Source:
    key: str
    label: str
    description: str
    model: str
    base_domain: list[Any]
    date_fields: list[str]  # el primero es el de por defecto; [] = la fuente no se filtra por fecha
    columns: list[str]  # columnas sugeridas al crear una consulta
    branch_field: str | None = None
    warehouse_field: str | None = None
    warehouse_match: Literal["id", "view_location", "rental_out_location"] = "id"
    company_field: str | None = "company_id"
    date_optional: bool = False
    # Campos no almacenados que Odoo resuelve barato (heredados del asiento en account.payment): se permiten como
    # columna, regla, periodo u orden, pero no para agrupar ni sumar.
    delegated: tuple[str, ...] = ()

    def mm(self) -> ModelMap:
        return ModelMap(model=self.model, fields={}, company_field=self.company_field, branch_field=self.branch_field,
                        warehouse_field=self.warehouse_field, warehouse_match=self.warehouse_match)


_CUSTOMER = ["out_invoice", "out_refund"]
_VENDOR = ["in_invoice", "in_refund"]
SOURCES: dict[str, Source] = {s.key: s for s in [
    Source("facturas_cliente", "Facturas de cliente",
           "Facturas y notas de crédito de cliente publicadas (montos en MXN; las notas de crédito en negativo).",
           "account.move", [("move_type", "in", _CUSTOMER), ("state", "=", "posted")],
           ["invoice_date", "invoice_date_due", "date"],
           ["name", "invoice_date", "commercial_partner_id", "branch_office_id", "invoice_user_id",
            "amount_untaxed_signed", "amount_total_signed", "amount_residual_signed", "payment_state"],
           branch_field="branch_office_id"),
    Source("lineas_factura", "Líneas de facturas de cliente",
           "Cada concepto facturado con su cuenta contable (p. ej. 401-001-001). Útil para ingresos por cuenta.",
           "account.move.line",
           [("parent_state", "=", "posted"), ("move_id.move_type", "in", _CUSTOMER), ("exclude_from_invoice_tab", "=", False)],
           ["date"],
           ["move_id", "date", "partner_id", "account_id", "product_id", "quantity", "price_subtotal", "balance"],
           branch_field="move_id.branch_office_id"),
    Source("movimientos_contables", "Movimientos contables (libro mayor)",
           "Todos los apuntes publicados: diario, cuenta, debe, haber y saldo.",
           "account.move.line", [("parent_state", "=", "posted")], ["date"],
           ["date", "move_id", "journal_id", "account_id", "partner_id", "name", "debit", "credit", "balance"],
           branch_field="move_id.branch_office_id"),
    Source("facturas_proveedor", "Facturas de proveedor",
           "Facturas y notas de crédito de proveedor publicadas.",
           "account.move", [("move_type", "in", _VENDOR), ("state", "=", "posted")],
           ["invoice_date", "invoice_date_due", "date"],
           ["name", "invoice_date", "commercial_partner_id", "ref", "amount_untaxed_signed", "amount_total_signed",
            "amount_residual_signed", "payment_state"],
           branch_field="branch_office_id"),
    Source("pagos", "Pagos y cobros", "Pagos recibidos y enviados publicados.",
           "account.payment", [("state", "=", "posted")], ["date"],
           ["name", "date", "partner_id", "partner_type", "payment_type", "journal_id", "amount", "currency_id", "ref"],
           delegated=("name", "date", "journal_id", "ref")),
    Source("ordenes_venta", "Órdenes de venta y renta", "Órdenes confirmadas (incluye las de renta).",
           "sale.order", [("state", "in", ["sale", "done"])], ["date_order"],
           ["name", "date_order", "partner_id", "user_id", "warehouse_id", "branch_office_id", "is_rental_so",
            "amount_untaxed", "invoice_status"],
           branch_field="branch_office_id", warehouse_field="warehouse_id"),
    Source("contratos_renta", "Contratos de renta", "Líneas de contrato de renta: equipo, periodo e importe.",
           "rental.order.contract", [], ["rental_start_time", "rental_end_time"],
           ["order_id", "partner_id", "product_id", "rental_start_time", "rental_end_time", "rental_qty",
            "price_subtotal", "state"],
           branch_field="order_id.branch_office_id", warehouse_field="location_dest_id",
           warehouse_match="rental_out_location"),
    Source("flota", "Flota (unidades)", "Unidades de Odoo › Flota con su estado y almacén actual.",
           "fleet.vehicle", [], ["acquisition_date", "create_date"],
           ["name", "license_plate", "model_id", "state_id", "current_warehouse_id", "acquisition_date"],
           warehouse_field="current_warehouse_id", date_optional=True),
    Source("compras", "Órdenes de compra", "Órdenes de compra confirmadas.",
           "purchase.order", [("state", "in", ["purchase", "done"])], ["date_approve", "date_order"],
           ["name", "date_approve", "partner_id", "user_id", "branch_office_id", "amount_untaxed", "invoice_status"],
           branch_field="branch_office_id", warehouse_field="picking_type_id.warehouse_id"),
    Source("mantenimiento", "Peticiones de mantenimiento", "Solicitudes de mantenimiento con etapa y equipo.",
           "maintenance.request", [], ["request_date", "close_date"],
           ["name", "request_date", "equipment_id", "maintenance_type", "stage_id", "close_date", "branch_office_id"],
           branch_field="branch_office_id"),
    Source("clientes", "Clientes", "Catálogo de clientes (contactos con ventas).",
           "res.partner", [("customer_rank", ">", 0)], ["create_date"],
           ["name", "vat", "city", "state_id", "user_id", "email", "phone"],
           company_field=None, date_optional=True),
]}


# ------------------------------------------------------------------ consulta
Op = Literal["=", "!=", "contiene", "no_contiene", ">", ">=", "<", "<=", "en", "vacio", "no_vacio"]
Preset = Literal["mes_actual", "mes_anterior", "trimestre_actual", "anio_actual", "anio_anterior", "ultimos_30",
                 "ultimos_90", "ultimos_365", "personalizado", "sin_fecha"]
OP_LABEL = {"=": "es igual a", "!=": "es distinto de", "contiene": "contiene", "no_contiene": "no contiene",
            ">": "mayor que", ">=": "mayor o igual a", "<": "menor que", "<=": "menor o igual a",
            "en": "es alguno de", "vacio": "está vacío", "no_vacio": "no está vacío"}
PRESET_LABEL = {"mes_actual": "Este mes", "mes_anterior": "Mes anterior", "trimestre_actual": "Este trimestre",
                "anio_actual": "Este año", "anio_anterior": "Año anterior", "ultimos_30": "Últimos 30 días",
                "ultimos_90": "Últimos 90 días", "ultimos_365": "Últimos 12 meses", "personalizado": "Personalizado",
                "sin_fecha": "Sin filtro de fecha"}


def _field(v: str) -> str:
    v = v.strip()
    if not FIELD_RE.match(v):
        raise ValueError(f"Campo inválido: {v!r}")
    return v


class Columna(BaseModel):
    campo: str
    etiqueta: str = Field("", max_length=60)

    @field_validator("campo")
    @classmethod
    def _c(cls, v: str) -> str:
        return _field(v)


class Filtro(BaseModel):
    campo: str
    op: Op = "="
    valor: str | float | bool | None = None

    @field_validator("campo")
    @classmethod
    def _c(cls, v: str) -> str:
        return _field(v)


class Periodo(BaseModel):
    campo: str = ""
    preset: Preset = "mes_actual"
    desde: date | None = None
    hasta: date | None = None


class Agrupar(BaseModel):
    campos: list[str] = Field(default_factory=list, max_length=3)  # «campo» o «campo:month» para fechas
    sumar: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("campos")
    @classmethod
    def _gb(cls, v: list[str]) -> list[str]:
        out = []
        for x in v:
            name, _, gran = x.partition(":")
            if gran and gran not in GRANULARITIES:
                raise ValueError(f"Agrupación de fecha inválida: {gran}")
            out.append(f"{_field(name)}:{gran}" if gran else _field(name))
        return out

    @field_validator("sumar")
    @classmethod
    def _sum(cls, v: list[str]) -> list[str]:
        return [_field(x) for x in v]


class Consulta(BaseModel):
    id: str | None = None
    nombre: str = Field(..., min_length=1, max_length=80)
    descripcion: str = Field("", max_length=300)
    fuente: str
    columnas: list[Columna] = Field(default_factory=list, max_length=40)
    filtros: list[Filtro] = Field(default_factory=list, max_length=15)
    periodo: Periodo = Field(default_factory=Periodo)
    agrupar: Agrupar = Field(default_factory=Agrupar)
    orden: str = Field("", max_length=80)
    compartida: bool = True

    @field_validator("fuente")
    @classmethod
    def _src(cls, v: str) -> str:
        if v not in SOURCES:
            raise ValueError(f"Fuente desconocida: {v}")
        return v

    @property
    def grouped(self) -> bool:
        return bool(self.agrupar.campos)


# ------------------------------------------------------------------ campos
def usable_fields(meta: dict[str, dict[str, Any]], delegated: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """Campos que se pueden pedir a Odoo sin cargarlo: almacenados (o delegados baratos) y de tipos simples."""
    return {k: v for k, v in meta.items()
            if v.get("type") in SIMPLE_TYPES and (v.get("store", True) or k in delegated) and k != "id"
            and not k.startswith("__")}


def field_list(meta: dict[str, dict[str, Any]], delegated: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for k, v in sorted(usable_fields(meta, delegated).items(), key=lambda kv: (kv[1].get("string") or kv[0]).lower()):
        out.append({"campo": k, "etiqueta": v.get("string") or k, "tipo": v.get("type"),
                    "agrupable": bool(v.get("store", True)),
                    "opciones": [[a, b] for a, b in (v.get("selection") or [])] or None,
                    "relacion": v.get("relation") or None, "personalizado": k.startswith("x_")})
    return out


def _label(meta: dict[str, dict[str, Any]], name: str) -> str:
    return (meta.get(name) or {}).get("string") or name


def validate(q: Consulta, meta: dict[str, dict[str, Any]]) -> None:
    src = SOURCES[q.fuente]
    ok = usable_fields(meta, src.delegated)
    stored = usable_fields(meta)

    def need(name: str, what: str, types: set[str] | None = None) -> dict[str, Any]:
        f = ok.get(name)
        if not f:
            raise ValueError(f"{what}: el campo «{name}» no existe en Odoo o no se puede consultar (no está almacenado)")
        if types and f["type"] not in types:
            raise ValueError(f"{what}: «{_label(meta, name)}» no se puede usar así (tipo {f['type']})")
        return f

    if not q.grouped and not q.columnas:
        raise ValueError("Elige al menos una columna")
    for c in q.columnas:
        need(c.campo, "Columna")
    for f in q.filtros:
        need(f.campo, "Filtro")
        if f.op not in ("vacio", "no_vacio") and (f.valor is None or f.valor == ""):
            raise ValueError(f"Filtro «{_label(meta, f.campo)}»: falta el valor")
    for g in q.agrupar.campos:
        name, _, gran = g.partition(":")
        f = need(name, "Agrupar", GROUPABLE)
        if name not in stored:
            raise ValueError(f"Agrupar: «{_label(meta, name)}» no se puede agrupar en Odoo; úsalo como columna o regla")
        if f["type"] in ("date", "datetime") and not gran:
            raise ValueError(f"Agrupar por «{_label(meta, name)}»: elige día, semana, mes, trimestre o año")
    for s in q.agrupar.sumar:
        need(s, "Sumar", NUMERIC)
        if s not in stored:
            raise ValueError(f"Sumar: «{_label(meta, s)}» no se puede sumar en Odoo")
    if q.orden:
        name = q.orden.split()[0]
        if not q.grouped:
            need(name, "Ordenar")
    p = q.periodo
    if p.preset != "sin_fecha":
        campo = p.campo or (src.date_fields[0] if src.date_fields else "")
        if not campo:
            raise ValueError("Esta fuente no se filtra por fecha: elige «Sin filtro de fecha»")
        need(campo, "Periodo", {"date", "datetime"})
    elif not src.date_optional:
        raise ValueError(f"«{src.label}» necesita un periodo (para no traer años completos de Odoo)")


def period_range(p: Periodo, today: date, desde: date | None = None, hasta: date | None = None) -> tuple[date, date] | None:
    if desde and hasta:
        rng = (desde, hasta)
    elif p.preset == "sin_fecha":
        return None
    elif p.preset == "mes_actual":
        rng = (today.replace(day=1), today)
    elif p.preset == "mes_anterior":
        end = today.replace(day=1) - timedelta(days=1)
        rng = (end.replace(day=1), end)
    elif p.preset == "trimestre_actual":
        rng = (date(today.year, (today.month - 1) // 3 * 3 + 1, 1), today)
    elif p.preset == "anio_actual":
        rng = (date(today.year, 1, 1), today)
    elif p.preset == "anio_anterior":
        rng = (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    elif p.preset in ("ultimos_30", "ultimos_90", "ultimos_365"):
        rng = (today - timedelta(days=int(p.preset.split("_")[1]) - 1), today)
    else:
        if not (p.desde and p.hasta):
            raise ValueError("Periodo personalizado: indica desde y hasta")
        rng = (p.desde, p.hasta)
    if rng[1] < rng[0]:
        raise ValueError("El periodo termina antes de empezar")
    if (rng[1] - rng[0]).days > 366 * MAX_YEARS:
        raise ValueError(f"El periodo máximo es de {MAX_YEARS} años")
    return rng


# ------------------------------------------------------------------ dominio
def _utc(d: date, tz: str) -> str:
    return datetime.combine(d, time.min, ZoneInfo(tz)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _date_terms(name: str, ftype: str, rng: tuple[date, date], tz: str) -> list[Any]:
    end = rng[1] + timedelta(days=1)
    if ftype == "datetime":
        return [(name, ">=", _utc(rng[0], tz)), (name, "<", _utc(end, tz))]
    return [(name, ">=", rng[0].isoformat()), (name, "<", end.isoformat())]


def _scalar(f: dict[str, Any], raw: Any, label: str, tz: str) -> Any:
    t = f["type"]
    if t in NUMERIC:
        try:
            v = float(str(raw).replace(",", "").replace("$", "").strip())
        except ValueError as exc:
            raise ValueError(f"Filtro «{label}»: «{raw}» no es un número") from exc
        return int(v) if t == "integer" and v.is_integer() else v
    if t == "boolean":
        return str(raw).strip().lower() in ("1", "true", "sí", "si", "verdadero", "x", "yes")
    if t in ("date", "datetime"):
        try:
            d = date.fromisoformat(str(raw).strip()[:10])
        except ValueError as exc:
            raise ValueError(f"Filtro «{label}»: usa fechas AAAA-MM-DD") from exc
        return _utc(d, tz) if t == "datetime" else d.isoformat()
    if t == "selection":
        text = str(raw).strip().lower()
        for key, lab in f.get("selection") or []:
            if text in (str(key).lower(), str(lab).lower()):
                return key
        return str(raw).strip()
    return str(raw).strip()


def filter_terms(flt: Filtro, meta: dict[str, dict[str, Any]], tz: str) -> list[Any]:
    f, name, label = meta[flt.campo], flt.campo, _label(meta, flt.campo)
    t = f["type"]
    if flt.op == "vacio":
        return [(name, "=", False)]
    if flt.op == "no_vacio":
        return [(name, "!=", False)]
    if flt.op in ("contiene", "no_contiene"):
        return [(name, "ilike" if flt.op == "contiene" else "not ilike", str(flt.valor).strip())]
    if flt.op == "en":
        parts = [p.strip() for p in str(flt.valor).split(",") if p.strip()]
        if not parts:
            raise ValueError(f"Filtro «{label}»: escribe los valores separados por coma")
        if t == "many2one":  # por nombre: cualquiera de ellos (en una cuenta, «401-001» busca por código)
            return ["|"] * (len(parts) - 1) + [(name, "ilike", p) for p in parts]
        return [(name, "in", [_scalar(f, p, label, tz) for p in parts])]
    if t == "many2one":
        if flt.op not in ("=", "!="):
            raise ValueError(f"Filtro «{label}»: usa «contiene», «es igual a» o «es alguno de»")
        v = str(flt.valor).strip()
        if v.isdigit():
            return [(name, flt.op, int(v))]
        return [(name, "ilike" if flt.op == "=" else "not ilike", v)]
    if t == "datetime" and flt.op in ("=", "!="):  # «el día X»
        d = date.fromisoformat(str(flt.valor).strip()[:10])
        rng = _date_terms(name, t, (d, d), tz)
        return rng if flt.op == "=" else ["|", (name, "<", rng[0][2]), (name, ">=", rng[1][2])]
    return [(name, flt.op, _scalar(f, flt.valor, label, tz))]


async def build_domain(odoo: OdooClient, settings: Settings, q: Consulta, meta: dict[str, dict[str, Any]],
                       scope: KpiFilters, rng: tuple[date, date] | None) -> list[Any]:
    src = SOURCES[q.fuente]
    ctx = Ctx(odoo=odoo, filters=scope, tz=settings.ODOO_TZ)
    dom: list[Any] = list(src.base_domain)
    # Términos del filtro base que la instancia no tiene (p. ej. campos de otra versión de Odoo) se omiten.
    dom = [t for t in dom if not isinstance(t, tuple) or t[0].split(".")[0] in meta]
    try:
        dom += await scope_domain(ctx, src.mm(), src.label)
    except ScopeRestricted as exc:
        raise PermissionError(f"«{src.label}» no se puede filtrar por tu sucursal/almacén: pide acceso completo.") from exc
    if rng:
        campo = q.periodo.campo or src.date_fields[0]
        dom += _date_terms(campo, meta[campo]["type"], rng, settings.ODOO_TZ)
    for flt in q.filtros:
        dom += filter_terms(flt, meta, settings.ODOO_TZ)
    return dom


# ------------------------------------------------------------------ formato
def _fmt(v: Any, f: dict[str, Any], tz: str) -> Any:
    t = f.get("type")
    if t == "boolean":
        return "Sí" if v else "No"
    if v is False or v is None:
        return None
    if t == "many2one":
        return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else str(v)
    if t == "selection":
        return dict((str(a), b) for a, b in (f.get("selection") or [])).get(str(v), v)
    if t == "datetime":
        try:
            d = datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
            return d.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return str(v)
    if t in ("float", "monetary"):
        return round(float(v), 2)
    return v


def col_type(ftype: str | None) -> str:
    return {"monetary": "money", "float": "number", "integer": "number", "date": "date", "datetime": "datetime"}.get(
        ftype or "", "text")


def _group_label(g: dict[str, Any], gb: str, meta: dict[str, dict[str, Any]], tz: str) -> Any:
    name, _, gran = gb.partition(":")
    v = g.get(gb)
    if gran:
        start = next((str(t[2]) for t in g.get("__domain") or []
                      if isinstance(t, (list, tuple)) and len(t) == 3 and t[0] == name and t[1] == ">="), None)
        if start and gran in ("day", "month", "year"):
            return start[:{"day": 10, "month": 7, "year": 4}[gran]]
        return v or None
    return _fmt(v, meta.get(name, {}), tz)


# ------------------------------------------------------------------ ejecución
class TooManyRows(ValueError):
    pass


def _scope_key(scope: KpiFilters) -> dict[str, Any]:
    return {k: getattr(scope, k) for k in ("company_id", "warehouse_id", "branch_id", "allowed_warehouse_ids",
                                           "allowed_branch_ids", "strict_scope")}


async def run(odoo: OdooClient, cache: Cache, settings: Settings, q: Consulta, scope: KpiFilters, *,
              preview: bool = False, refresh: bool = False, desde: date | None = None, hasta: date | None = None,
              today: date | None = None) -> dict[str, Any]:
    src = SOURCES[q.fuente]
    meta = await odoo.fields_get(src.model, lang=settings.DATA_LANG or None)
    validate(q, meta)
    today = today or datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
    rng = period_range(q.periodo, today, desde, hasta)
    body = q.model_dump(mode="json", include={"fuente", "columnas", "filtros", "periodo", "agrupar", "orden"})
    key = make_key("datos-v2", body, rng, _scope_key(scope), preview, settings.DATA_EXPORT_MAX_ROWS, settings.DATA_LANG)
    if refresh and await cache.get_json(f"{key}:refrescado"):
        refresh = False  # ya se actualizó hace poco: se sirve lo que hay
    if not refresh:
        hit = await cache.get_json(key)
        if hit:
            return {**hit, "desde_cache": True}
    out = await _execute(odoo, settings, q, src, meta, scope, rng, preview)
    await cache.set_json(key, out, max(seconds_until_sync(settings), 300))
    if refresh:
        await cache.set_json(f"{key}:refrescado", True, REFRESH_EVERY)
    return {**out, "desde_cache": False}


async def _execute(odoo: OdooClient, settings: Settings, q: Consulta, src: Source, meta: dict[str, dict[str, Any]],
                   scope: KpiFilters, rng: tuple[date, date] | None, preview: bool) -> dict[str, Any]:
    tz = settings.ODOO_TZ
    dom = await build_domain(odoo, settings, q, meta, scope, rng)
    labels = {c.campo: c.etiqueta for c in q.columnas if c.etiqueta}
    cap = min(settings.DATA_EXPORT_MAX_ROWS, settings.ODOO_MAX_RECORDS)
    if q.grouped:
        sums = [s for s in q.agrupar.sumar]
        groups = await odoo.read_group(src.model, dom, [f"{s}:sum" for s in sums], q.agrupar.campos,
                                       limit=MAX_GROUPS + 1, lang=settings.DATA_LANG or None)
        cols = [{"key": f"g{i}", "label": labels.get(g.split(":")[0]) or _label(meta, g.split(":")[0]) + (
            f" ({GRANULARITIES[g.split(':')[1]].lower()})" if ":" in g else ""),
                 "type": "text"} for i, g in enumerate(q.agrupar.campos)]
        cols += [{"key": s, "label": labels.get(s) or _label(meta, s), "type": col_type(meta[s]["type"])} for s in sums]
        cols.append({"key": "__count", "label": "Registros", "type": "number"})
        rows = []
        for g in groups[:MAX_GROUPS]:
            r: dict[str, Any] = {f"g{i}": _group_label(g, gb, meta, tz) for i, gb in enumerate(q.agrupar.campos)}
            for s in sums:
                r[s] = round(float(g.get(s) or 0), 2)
            r["__count"] = int(g.get("__count") or 0)
            rows.append(r)
        order = q.orden.split()
        if order:
            k = order[0] if order[0] in sums or order[0] == "__count" else None
            if k:
                rows.sort(key=lambda r: r.get(k) or 0, reverse=len(order) > 1 and order[1].lower() == "desc")
        elif sums:
            rows.sort(key=lambda r: r.get(sums[0]) or 0, reverse=True)
        totals = {s: round(sum(r[s] for r in rows), 2) for s in sums}
        totals["__count"] = sum(r["__count"] for r in rows)
        total = totals["__count"]
        shown = rows[:PREVIEW_ROWS] if preview else rows
        truncated = len(groups) > MAX_GROUPS
    else:
        total = await odoo.search_count(src.model, dom)
        if not preview and total > cap:
            raise TooManyRows(f"La consulta trae {total:,} registros y el tope es {cap:,}. Acota el periodo o agrega "
                              "filtros (o agrúpala para ver totales).")
        names = [c.campo for c in q.columnas]
        order = q.orden or (f"{q.periodo.campo or (src.date_fields[0] if src.date_fields else 'id')} desc")
        data = await odoo.search_read(src.model, dom, names, order=order, limit=PREVIEW_ROWS if preview else cap,
                                      lang=settings.DATA_LANG or None)
        cols = [{"key": n, "label": labels.get(n) or _label(meta, n), "type": col_type(meta[n]["type"])} for n in names]
        shown = [{n: _fmt(r.get(n), meta[n], tz) for n in names} for r in data]
        totals = {n: round(sum(r[n] or 0 for r in shown), 2) for n in names if meta[n]["type"] in ("monetary", "float")}
        truncated = False
    return {
        "columnas": cols, "filas": shown, "total": total, "totales": totals, "vista_previa": preview,
        "agrupado": q.grouped, "grupos": len(rows) if q.grouped else 0,
        "truncado": truncated, "fuente": src.label, "modelo": src.model,
        "periodo": {"desde": rng[0].isoformat(), "hasta": rng[1].isoformat()} if rng else None,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def describe(q: Consulta, meta: dict[str, dict[str, Any]], rng: tuple[date, date] | None) -> list[tuple[str, str]]:
    """Parámetros de la consulta en palabras (hoja «Parámetros» del Excel)."""
    src = SOURCES[q.fuente]
    out = [("Consulta", q.nombre), ("Descripción", q.descripcion or "—"), ("Fuente", f"{src.label} ({src.model})")]
    if rng:
        campo = q.periodo.campo or src.date_fields[0]
        out.append(("Periodo", f"{PRESET_LABEL[q.periodo.preset]}: {rng[0]:%d/%m/%Y} – {rng[1]:%d/%m/%Y} "
                               f"(por «{_label(meta, campo)}»)"))
    else:
        out.append(("Periodo", "Sin filtro de fecha"))
    for f in q.filtros:
        opts = {str(k): v for k, v in (meta.get(f.campo) or {}).get("selection") or []}
        shown = ", ".join(opts.get(x.strip(), x.strip()) for x in str(f.valor).split(",")) if f.op == "en" else opts.get(str(f.valor), f.valor)
        val = "" if f.op in ("vacio", "no_vacio") else f" {shown}"
        out.append(("Filtro", f"{_label(meta, f.campo)} {OP_LABEL[f.op]}{val}"))
    if q.grouped:
        out.append(("Agrupado por", ", ".join(_label(meta, g.split(":")[0]) for g in q.agrupar.campos)))
        if q.agrupar.sumar:
            out.append(("Sumas", ", ".join(_label(meta, s) for s in q.agrupar.sumar)))
    out.append(("Filtro base de la fuente", src.description))
    return out


def build_xlsx(q: Consulta, result: dict[str, Any], params: list[tuple[str, str]], settings: Settings,
               user: str) -> bytes:
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Datos"
    head_fill, head_font = PatternFill("solid", fgColor="154046"), Font(bold=True, color="FFFFFF")
    cols = result["columnas"]
    ws.append([c["label"] for c in cols])
    for cell in ws[1]:
        cell.fill, cell.font = head_fill, head_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r in result["filas"]:
        row = []
        for c in cols:
            v = r.get(c["key"])
            if v is not None and c["type"] == "date":
                try:
                    v = date.fromisoformat(str(v))
                except ValueError:
                    pass
            elif v is not None and c["type"] == "datetime":
                try:
                    v = datetime.strptime(str(v), "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            row.append(v)
        ws.append(row)
    fmt = {"money": "#,##0.00", "number": "#,##0.##", "date": "dd/mm/yyyy", "datetime": "dd/mm/yyyy hh:mm"}
    n = len(result["filas"])
    for i, c in enumerate(cols, start=1):
        col = get_column_letter(i)
        if c["type"] in fmt:
            for cell in ws[col][1:]:
                cell.number_format = fmt[c["type"]]
        sample = [len(str(r.get(c["key"]) or "")) for r in result["filas"][:300]]
        ws.column_dimensions[col].width = min(50, max(10, len(c["label"]) + 2, *(x + 2 for x in sample)))
    if n and any(c["type"] in ("money", "number") for c in cols):  # fila de totales con fórmulas
        ws.append([])
        total_row = n + 3
        ws.cell(row=total_row, column=1, value="Total").font = Font(bold=True)
        for i, c in enumerate(cols, start=1):
            if c["type"] in ("money", "number") and i > 1:
                col = get_column_letter(i)
                cell = ws.cell(row=total_row, column=i, value=f"=SUBTOTAL(9,{col}2:{col}{n + 1})")
                cell.font, cell.number_format = Font(bold=True), fmt[c["type"]]
    ws.freeze_panes = "A2"
    if n:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{n + 1}"

    p = wb.create_sheet("Parámetros")
    now = datetime.now(ZoneInfo(settings.ODOO_TZ))
    for k, v in [*params, ("Registros", f"{result['total']:,}"), ("Generado", f"{now:%d/%m/%Y %H:%M} (hora de México)"),
                 ("Generado por", user), ("Origen", "Odoo (solo lectura) · PROSIC BI")]:
        p.append([k, v])
    for cell in p["A"]:
        cell.font = Font(bold=True)
    p.column_dimensions["A"].width, p.column_dimensions["B"].width = 26, 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ consultas guardadas
_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_query (
    id TEXT PRIMARY KEY, doc TEXT NOT NULL, owner TEXT NOT NULL, shared INTEGER NOT NULL,
    updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""
SYSTEM_OWNER = "sistema"

# Consultas iniciales: lo que Finanzas suele pedir. Se pueden editar o borrar.
STARTERS: list[dict[str, Any]] = [
    {"nombre": "Ingresos por cuenta contable", "fuente": "lineas_factura",
     "descripcion": "Lo facturado sin IVA en el periodo por cuenta 401 (ingresos) y 402 (notas de crédito). "
                    "En «Saldo» el ingreso aparece en negativo, como en la contabilidad.",
     "periodo": {"preset": "mes_actual"}, "filtros": [{"campo": "account_id", "op": "en", "valor": "401, 402"}],
     "agrupar": {"campos": ["account_id"], "sumar": ["price_subtotal", "balance"]},
     "columnas": [{"campo": "account_id"}, {"campo": "balance"}, {"campo": "price_subtotal"}]},
    {"nombre": "Facturas emitidas del mes", "fuente": "facturas_cliente",
     "descripcion": "Detalle de facturas y notas de crédito de cliente con su saldo pendiente.",
     "periodo": {"preset": "mes_actual"}, "columnas": [{"campo": c} for c in SOURCES["facturas_cliente"].columns]},
    {"nombre": "Cartera pendiente por cliente", "fuente": "facturas_cliente",
     "descripcion": "Saldo por cobrar de facturas de los últimos 12 meses, por cliente.",
     "periodo": {"preset": "ultimos_365"},
     "filtros": [{"campo": "amount_residual_signed", "op": "!=", "valor": 0}],
     "agrupar": {"campos": ["commercial_partner_id"], "sumar": ["amount_residual_signed", "amount_total_signed"]},
     "columnas": [{"campo": "commercial_partner_id"}, {"campo": "amount_residual_signed"}]},
    {"nombre": "Cobros del mes", "fuente": "pagos", "descripcion": "Pagos recibidos de clientes en el periodo.",
     "periodo": {"preset": "mes_actual"}, "filtros": [{"campo": "payment_type", "op": "=", "valor": "inbound"}],
     "columnas": [{"campo": c} for c in ["name", "date", "partner_id", "journal_id", "amount", "ref"]]},
    {"nombre": "Flota por estado y almacén", "fuente": "flota", "descripcion": "Unidades por estado en cada almacén.",
     "periodo": {"preset": "sin_fecha"}, "agrupar": {"campos": ["current_warehouse_id", "state_id"], "sumar": []},
     "columnas": [{"campo": "current_warehouse_id"}, {"campo": "state_id"}]},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueryStore:
    def __init__(self, path: str):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            if not self._conn.execute("SELECT 1 FROM data_meta WHERE k = 'seeded'").fetchone():
                for doc in STARTERS:
                    q = Consulta.model_validate(doc)
                    q.id = uuid.uuid4().hex[:12]
                    self._conn.execute("INSERT INTO data_query VALUES (?, ?, ?, 1, ?, ?)",
                                       (q.id, q.model_dump_json(), SYSTEM_OWNER, _now(), SYSTEM_OWNER))
                self._conn.execute("INSERT INTO data_meta VALUES ('seeded', '1')")

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    @staticmethod
    def _out(r: sqlite3.Row) -> dict[str, Any]:
        doc = json.loads(r["doc"])
        return {**doc, "id": r["id"], "propietario": r["owner"], "compartida": bool(r["shared"]),
                "actualizada": r["updated_at"], "actualizada_por": r["updated_by"]}

    def visible(self, user: str) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM data_query WHERE shared = 1 OR owner = ? ORDER BY updated_at DESC", (user,))
        return [self._out(r) for r in rows]

    def get(self, qid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM data_query WHERE id = ?", (qid,))
        return self._out(rows[0]) if rows else None

    def save(self, q: Consulta, user: str, is_admin: bool) -> dict[str, Any]:
        cur = self.get(q.id) if q.id else None
        if cur and not (is_admin or cur["propietario"] in (user, SYSTEM_OWNER)):
            raise PermissionError("Solo quien creó la consulta (o un administrador) puede cambiarla; guárdala como copia.")
        q = q.model_copy(update={"id": cur["id"] if cur else uuid.uuid4().hex[:12]})
        owner = cur["propietario"] if cur and cur["propietario"] != SYSTEM_OWNER else user
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO data_query VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET doc = excluded.doc, "
                "owner = excluded.owner, shared = excluded.shared, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (q.id, q.model_dump_json(exclude={"id"}), owner, int(q.compartida), _now(), user))
        return self.get(q.id)  # type: ignore[return-value, arg-type]

    def delete(self, qid: str, user: str, is_admin: bool) -> bool:
        cur = self.get(qid)
        if not cur:
            return False
        if not (is_admin or cur["propietario"] in (user, SYSTEM_OWNER)):
            raise PermissionError("Solo quien creó la consulta (o un administrador) puede borrarla.")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM data_query WHERE id = ?", (qid,))
        return True

    def clear(self) -> None:  # pruebas
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM data_query")


@lru_cache(maxsize=4)
def get_query_store(path: str) -> QueryStore:
    return QueryStore(path)


# ------------------------------------------------------------------ sincronización nocturna (estado)
SYNC_MODULES: dict[str, str] = {
    "ventas": "Ventas y facturación", "comisiones": "Comisiones", "rentas": "Rentas e inventario",
    "mantenimiento": "Mantenimiento", "compras": "Compras", "pronostico": "Pronóstico",
}
_status: dict[str, dict[str, Any]] = {}
_status_lock = threading.Lock()


def nightly_modules(settings: Settings) -> list[str]:
    chosen = {x.strip() for x in (settings.NIGHTLY_MODULES or "").split(",") if x.strip()}
    return [m for m in SYNC_MODULES if m in chosen]


def _status_store(settings: Settings) -> QueryStore:
    return get_query_store(settings.HISTORY_DB_PATH)


def record_status(settings: Settings, module: str, ok: bool, seconds: float, error: str | None, kind: str) -> None:
    entry = {"ts": _now(), "ok": ok, "segundos": round(seconds, 1), "error": (error or "")[:300] or None, "tipo": kind}
    with _status_lock:
        _status[module] = entry
    st = _status_store(settings)
    with st._lock, st._conn:  # noqa: SLF001
        st._conn.execute("INSERT INTO data_meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",  # noqa: SLF001
                         (f"sync:{module}", json.dumps(entry)))


def sync_status(settings: Settings) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in _status_store(settings)._rows("SELECT k, v FROM data_meta WHERE k LIKE 'sync:%'"):  # noqa: SLF001
        out[r["k"][5:]] = json.loads(r["v"])
    with _status_lock:
        out.update({k: v for k, v in _status.items() if k not in out or v["ts"] > out[k]["ts"]})
    return out


async def timed(settings: Settings, module: str, kind: str, coro: Any) -> Any:
    """Ejecuta un precálculo y deja registrado si funcionó y cuánto tardó."""
    start = asyncio.get_running_loop().time()
    try:
        res = await coro
    except Exception as exc:  # noqa: BLE001
        record_status(settings, module, False, asyncio.get_running_loop().time() - start, str(exc), kind)
        raise
    record_status(settings, module, True, asyncio.get_running_loop().time() - start, None, kind)
    return res
