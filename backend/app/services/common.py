"""Utilidades compartidas por los servicios de KPIs: dominios con filtros,
series mensuales, antigüedad de saldos y manejo de secciones no disponibles."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, TypeVar
from zoneinfo import ZoneInfo

from ..odoo_client import OdooClient, OdooError, OdooModelUnavailable, OdooRpcError
from ..odoo_mapping import WAREHOUSE, ModelMap
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, KpiFilters, Table, Unit

log = logging.getLogger("prosic.kpis")
T = TypeVar("T")

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_EN_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
SIN_ASIGNAR = "Sin asignar"


class ModuleUnavailable(Exception):
    """El módulo completo no se puede calcular (modelo principal ausente)."""


class ScopeRestricted(Exception):
    """La sección no puede filtrarse por la sucursal/almacén del usuario: se oculta (no se muestran datos globales)."""


@dataclass
class Ctx:
    odoo: OdooClient
    filters: KpiFilters
    tz: str
    notes: list[str] = field(default_factory=list)
    history: Any = None  # services.history.History (opcional)
    _warehouse: dict[str, Any] | None = None
    _warehouses: list[dict[str, Any]] | None = None

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    async def warehouses(self) -> list[dict[str, Any]]:
        if self._warehouses is None:
            fields = ["id", "name", "company_id", WAREHOUSE.f("view_location"), WAREHOUSE.f("lot_stock")]
            if WAREHOUSE.has("rental_out_location"):
                missing = await self.odoo.missing_fields(WAREHOUSE.model, [WAREHOUSE.f("rental_out_location")])
                if not missing:
                    fields.append(WAREHOUSE.f("rental_out_location"))
            self._warehouses = await self.odoo.search_read(WAREHOUSE.model, [], fields, order="sequence, id")
        return self._warehouses

    async def warehouse(self) -> dict[str, Any] | None:
        wid = self.filters.warehouse_id
        if not wid:
            return None
        for w in await self.warehouses():
            if w["id"] == wid:
                return w
        raise OdooError(f"El almacén {wid} no existe o no es visible para el usuario de lectura")

    def warehouse_ids(self) -> list[int]:
        """Almacenes a los que se limita la consulta (filtro elegido o alcance del usuario); [] = todos."""
        f = self.filters
        return [f.warehouse_id] if f.warehouse_id else list(f.allowed_warehouse_ids or [])

    def branch_ids(self) -> list[int]:
        f = self.filters
        return [f.branch_id] if f.branch_id else list(f.allowed_branch_ids or [])

    async def scoped_warehouses(self) -> list[dict[str, Any]]:
        ids = set(self.warehouse_ids())
        return [w for w in await self.warehouses() if w["id"] in ids]

    def restrict(self, label: str, dimension: str) -> None:
        """Sección sin filtro posible por la dimensión del alcance: en modo estricto se oculta."""
        if self.filters.strict_scope:
            raise ScopeRestricted(f"«{label}» no se puede filtrar por {dimension}: se oculta para tu acceso.")
        self.note(f"El filtro de {dimension} no aplica a «{label}».")


# --------------------------------------------------------------- fechas / dominio
def _utc_str(d: date, tz: str) -> str:
    local = datetime.combine(d, time.min, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def date_domain(ctx: Ctx, mm: ModelMap, date_from: date, date_to: date, field_name: str | None = None) -> list[Any]:
    fname = field_name or mm.date_field
    if not fname:
        return []
    ftype = (await ctx.odoo.fields_get(mm.model)).get(fname, {}).get("type", "date")
    end_excl = date_to + timedelta(days=1)
    if ftype == "datetime":
        return [(fname, ">=", _utc_str(date_from, ctx.tz)), (fname, "<", _utc_str(end_excl, ctx.tz))]
    return [(fname, ">=", date_from.isoformat()), (fname, "<", end_excl.isoformat())]


async def scope_domain(ctx: Ctx, mm: ModelMap, label: str) -> list[Any]:
    """Filtros de compañía, sucursal y almacén según el mapeo del modelo."""
    f = ctx.filters
    dom: list[Any] = []
    if f.company_id and mm.company_field:
        dom.append((mm.company_field, "=", f.company_id))
    branches = ctx.branch_ids()
    if branches:
        if mm.branch_field:
            dom.append((mm.branch_field, "in", branches))
        else:
            ctx.restrict(label, "sucursal")
    wh_ids = ctx.warehouse_ids()
    if wh_ids:
        if not mm.warehouse_field:
            ctx.restrict(label, "almacén")
        else:
            if f.warehouse_id:
                await ctx.warehouse()  # valida que exista
            whs = await ctx.scoped_warehouses()
            if mm.warehouse_match == "id":
                dom.append((mm.warehouse_field, "in", wh_ids))
            elif mm.warehouse_match == "view_location":
                dom.append((mm.warehouse_field, "child_of",
                            [m2o_id(w.get(WAREHOUSE.f("view_location"))) for w in whs] or [0]))
            else:
                locs = [m2o_id(w.get(WAREHOUSE.fields.get("rental_out_location") or "", False)) for w in whs]
                dom.append((mm.warehouse_field, "in", [x for x in locs if x] or [0]))
    return dom


async def build_domain(
    ctx: Ctx, mm: ModelMap, label: str, *, period: tuple[date, date] | None = None, extra: list[Any] | None = None
) -> list[Any]:
    dom = list(mm.base_domain) + (extra or [])
    dom += await scope_domain(ctx, mm, label)
    if period:
        dom += await date_domain(ctx, mm, *period)
    return dom


# --------------------------------------------------------------- grupos
def m2o_id(v: Any) -> int | None:
    return int(v[0]) if isinstance(v, (list, tuple)) and v else None


def m2o_label(v: Any) -> str:
    if isinstance(v, (list, tuple)) and len(v) > 1:
        return str(v[1])
    if v in (False, None, ""):
        return SIN_ASIGNAR
    return str(v)


def num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def group_month_key(group: dict[str, Any], gb: str) -> str | None:
    """'YYYY-MM' a partir de un grupo de read_group con groupby 'campo:month'."""
    label = group.get(gb)
    if isinstance(label, str):
        m = re.match(r"([A-Za-z]+)\s+(\d{4})", label.strip())
        if m and m.group(1).lower() in _EN_MONTHS:
            return f"{m.group(2)}-{_EN_MONTHS[m.group(1).lower()]:02d}"
    field_name = gb.split(":")[0]
    for term in group.get("__domain", []) or []:
        if isinstance(term, (list, tuple)) and len(term) == 3 and term[0] == field_name and term[1] == ">=":
            return str(term[2])[:7]
    return None


def months_between(d1: date, d2: date) -> list[str]:
    out, y, m = [], d1.year, d1.month
    while (y, m) <= (d2.year, d2.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{MESES[int(m) - 1]} {y}"


async def monthly(ctx: Ctx, mm: ModelMap, label: str, value_field: str, period: tuple[date, date],
                  extra: list[Any] | None = None, date_field: str | None = None) -> dict[str, float]:
    dfield = date_field or mm.date_field
    assert dfield
    dom = list(mm.base_domain) + (extra or []) + await scope_domain(ctx, mm, label)
    dom += await date_domain(ctx, mm, *period, field_name=dfield)
    gb = f"{dfield}:month"
    groups = await ctx.odoo.read_group(mm.model, dom, [f"{value_field}:sum"], [gb])
    out: dict[str, float] = {}
    for g in groups:
        k = group_month_key(g, gb)
        if k:
            out[k] = out.get(k, 0.0) + num(g.get(value_field))
    return out


def monthly_chart(key: str, title: str, cur: dict[str, float], period: tuple[date, date],
                  prev: dict[str, float] | None = None, prev_period: tuple[date, date] | None = None,
                  unit: Unit = "MXN", chart_type: str = "line") -> Chart:
    months = months_between(*period)
    prev_months = months_between(*prev_period) if prev_period else []
    data = []
    for i, mk in enumerate(months):
        row: dict[str, Any] = {"label": month_label(mk), "periodo": mk, "actual": round(cur.get(mk, 0.0), 2)}
        if prev is not None:
            row["anterior"] = round(prev.get(prev_months[i], 0.0), 2) if i < len(prev_months) else None
        data.append(row)
    series = [ChartSeries(key="actual", label="Periodo actual")]
    if prev is not None:
        series.append(ChartSeries(key="anterior", label="Periodo anterior"))
    return Chart(key=key, title=title, type=chart_type, unit=unit, series=series, data=data)  # type: ignore[arg-type]


def top_groups(groups: list[dict[str, Any]], gb: str, value_field: str, n: int = 10,
               other: bool = False) -> list[dict[str, Any]]:
    rows = [{"label": m2o_label(g.get(gb)), "id": m2o_id(g.get(gb)), "valor": round(num(g.get(value_field)), 2),
             "conteo": int(g.get("__count", 0))} for g in groups]
    rows.sort(key=lambda r: r["valor"], reverse=True)
    if other and len(rows) > n:
        rest = rows[n - 1:]
        rows = rows[: n - 1] + [{"label": "Otros", "id": None, "valor": round(sum(r["valor"] for r in rest), 2),
                                 "conteo": sum(r["conteo"] for r in rest)}]
    return rows[:n]


def bar_chart(key: str, title: str, rows: list[dict[str, Any]], unit: Unit = "MXN", entity: Any = None,
              chart_type: str = "hbar", value_label: str = "Monto") -> Chart:
    return Chart(key=key, title=title, type=chart_type, unit=unit, entity=entity,  # type: ignore[arg-type]
                 series=[ChartSeries(key="valor", label=value_label)],
                 data=[{"label": r["label"], "valor": r["valor"]} for r in rows])


# --------------------------------------------------------------- tarjetas
def delta_pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def card(key: str, label: str, cur: float | None, prev: float | None = None, unit: Unit = "MXN",
         higher_is_better: bool = True, hint: str | None = None) -> KpiCard:
    r = (lambda x: None if x is None else round(x, 2))
    return KpiCard(key=key, label=label, value=r(cur), previous=r(prev), delta_pct=delta_pct(cur, prev),
                   unit=unit, higher_is_better=higher_is_better, hint=hint)


async def total(ctx: Ctx, mm: ModelMap, label: str, value_field: str, period: tuple[date, date] | None,
                extra: list[Any] | None = None) -> tuple[float, int]:
    dom = await build_domain(ctx, mm, label, period=period, extra=extra)
    groups = await ctx.odoo.read_group(mm.model, dom, [f"{value_field}:sum"], [])
    if not groups:
        return 0.0, 0
    return num(groups[0].get(value_field)), int(groups[0].get("__count", 0))


# --------------------------------------------------------------- secciones seguras
async def safe(ctx: Ctx, title: str, aw: Awaitable[T]) -> T | None:
    """Ejecuta una sección; si el modelo/campo no existe, deja una nota en vez de romper."""
    try:
        return await aw
    except ScopeRestricted as exc:
        ctx.note(str(exc))
    except OdooModelUnavailable as exc:
        ctx.note(f"«{title}» no disponible: {exc.model} — {exc.reason}.")
    except OdooRpcError as exc:
        log.warning("Sección '%s' falló en Odoo: %s", title, exc)
        ctx.note(f"«{title}» no disponible: Odoo respondió «{exc.message[:160]}». Revisa odoo_mapping.py.")
    except KeyError as exc:
        ctx.note(f"«{title}» desactivada en el mapeo: {exc}.")
    return None


async def ensure_model(ctx: Ctx, mm: ModelMap) -> str | None:
    """None si el modelo y sus campos mapeados existen; si no, el motivo."""
    try:
        missing = await ctx.odoo.missing_fields(mm.model, mm.physical_fields())
    except OdooModelUnavailable as exc:
        return f"{exc.model}: {exc.reason}"
    if missing:
        return f"{mm.model}: faltan los campos {', '.join(missing)} (ajusta odoo_mapping.py)"
    return None


# --------------------------------------------------------------- antigüedad
AGING_BUCKETS = [
    ("por_vencer", "Por vencer", None, 0),
    ("d0_30", "1-30 días", 1, 30),
    ("d31_60", "31-60 días", 31, 60),
    ("d61_90", "61-90 días", 61, 90),
    ("d90_mas", "+90 días", 91, None),
]


async def aging(ctx: Ctx, mm: ModelMap, label: str, move_types: list[str], entity: Any,
                title: str, key: str) -> tuple[dict[str, float], Table, Chart, date]:
    """Antigüedad de saldos al corte (date_to o hoy, lo que sea menor) usando el saldo pendiente actual."""
    today = date.today()
    as_of = min(ctx.filters.date_to, today)
    if ctx.filters.date_to < today:
        ctx.note("La antigüedad de saldos usa el saldo pendiente ACTUAL de los documentos emitidos hasta la fecha de corte.")
    residual, due, partner = mm.f("residual"), mm.f("due_date"), mm.f("partner")
    base = await build_domain(ctx, mm, label, extra=[
        (mm.f("move_type"), "in", move_types), (residual, "!=", 0), (mm.date_field, "<=", as_of.isoformat())])
    totals: dict[str, float] = {}
    by_partner: dict[str, dict[str, Any]] = {}
    for bkey, _blabel, dmin, dmax in AGING_BUCKETS:
        dom = list(base)
        if dmin is None:  # no vencido: vence hoy o después
            dom.append((due, ">=", as_of.isoformat()))
        else:
            dom.append((due, "<=", (as_of - timedelta(days=dmin)).isoformat()))
            if dmax is not None:
                dom.append((due, ">=", (as_of - timedelta(days=dmax)).isoformat()))
        groups = await ctx.odoo.read_group(mm.model, dom, [f"{residual}:sum"], [partner])
        bucket_total = 0.0
        for g in groups:
            val = abs(num(g.get(residual)))
            bucket_total += val
            name = m2o_label(g.get(partner))
            row = by_partner.setdefault(name, {"_id": m2o_id(g.get(partner)), "label": name,
                                               **{b[0]: 0.0 for b in AGING_BUCKETS}})
            row[bkey] += val
        totals[bkey] = round(bucket_total, 2)
    for row in by_partner.values():
        row["total"] = round(sum(row[b[0]] for b in AGING_BUCKETS), 2)
        row["vencido"] = round(row["total"] - row["por_vencer"], 2)
    top = sorted(by_partner.values(), key=lambda r: r["vencido"], reverse=True)[:10]
    columns = [Column(key="label", label="Tercero", entity=entity)] + [
        Column(key=b[0], label=b[1], type="money") for b in AGING_BUCKETS] + [Column(key="total", label="Total", type="money")]
    table = Table(key=f"{key}_por_tercero", title=f"{title}: top 10 con saldo vencido", columns=columns,
                  odoo_model="res.partner",
                  rows=[{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "vencido"} for r in top])
    chart = Chart(key=f"{key}_antiguedad", title=f"{title} por antigüedad (corte {as_of.isoformat()})", type="bar",
                  series=[ChartSeries(key="valor", label="Saldo")],
                  data=[{"label": b[1], "valor": totals[b[0]]} for b in AGING_BUCKETS])
    return totals, table, chart, as_of


def periods(f: KpiFilters) -> tuple[tuple[date, date], tuple[date, date] | None]:
    prev = f.previous()
    return (f.date_from, f.date_to), ((prev.date_from, prev.date_to) if prev else None)
