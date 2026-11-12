"""KPIs de Rentas / inventario de equipos (módulo de rentas de terceros de PROSIC).

Estatus de cada unidad (foto de hoy, desde stock.quant):
- rentado:        en la «Rent Out Location» de un almacén
- retorno:        regresó de renta y espera inspección (ubicación «Rent In»)
- mantenimiento:  ubicación de cuarentena/taller (MAINTENANCE_LOCATION_KEYWORDS)
- disponible:     resto de ubicaciones internas
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, Table
from .common import (
    Ctx,
    ModuleUnavailable,
    build_domain,
    card,
    ensure_model,
    m2o_id,
    m2o_label,
    monthly,
    monthly_chart,
    num,
    periods,
    safe,
    total,
)

Result = tuple[list[KpiCard], list[Chart], list[Table]]
ALL_STATUSES = [("disponible", "Disponible"), ("retorno", "En retorno / inspección"),
                ("mantenimiento", "En mantenimiento"), ("rentado", "Rentado")]


def statuses() -> list[tuple[str, str]]:
    """«retorno» solo aplica si hay ubicaciones de inspección configuradas en el mapeo."""
    return [s for s in ALL_STATUSES if s[0] != "retorno" or M.RETURN_LOCATION_KEYWORDS]
AGE_BUCKETS = [("0-30 días", 0, 30), ("31-60 días", 31, 60), ("61-90 días", 61, 90), ("91-180 días", 91, 180),
               ("+180 días", 181, None)]
RETURN_WEEKS = [("Próximos 7 días", 0, 7), ("8-14 días", 8, 14), ("15-21 días", 15, 21), ("22-30 días", 22, 30)]
EXPIRING_WINDOWS = (7, 15, 30)
DETAIL_LIMIT = 500


def _parse_dt(value: Any, tz: str) -> datetime | None:
    if not value:
        return None
    dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz))


def _short(name: str) -> str:
    """'[PLAT. ART. ELEC. 34 PIES] Plataforma Articulada 34 Pies, Eléctrica' -> sin la referencia interna."""
    return name.split("] ", 1)[1] if name.startswith("[") and "] " in name else name


async def _product_categories(ctx: Ctx, product_ids: set[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    cat_field = M.PRODUCT.f("category")
    rows = await ctx.odoo.search_read(M.PRODUCT.model, [("id", "in", sorted(product_ids))], ["id", cat_field],
                                      limit=len(product_ids))
    return {r["id"]: m2o_label(r.get(cat_field)) for r in rows}


# ------------------------------------------------------------------ foto de hoy
async def snapshot(ctx: Ctx) -> dict[str, Any]:
    """Unidades por almacén / estatus / producto / serie a partir de stock.quant."""
    warehouses = await ctx.warehouses()
    view_f = M.WAREHOUSE.f("view_location")
    ro_f = M.WAREHOUSE.fields.get("rental_out_location") or ""
    rental_out: dict[int, dict[str, Any]] = {}
    for w in warehouses:
        loc = m2o_id(w.get(ro_f)) if ro_f else None
        if loc:
            rental_out[loc] = w
    if not rental_out:
        ctx.note("Ningún almacén tiene «Rental out Location» configurada: no se pueden contar unidades rentadas en inventario.")

    lm = M.LOCATION
    locs = await ctx.odoo.search_read(
        lm.model, ["|", (lm.f("usage"), "=", "internal"), ("id", "in", list(rental_out) or [0])],
        ["id", lm.f("name"), lm.f("usage"), lm.f("parent_path")])
    loc_by_id = {l["id"]: l for l in locs}
    maint_kw = [k.lower() for k in M.MAINTENANCE_LOCATION_KEYWORDS]
    return_kw = [k.lower() for k in M.RETURN_LOCATION_KEYWORDS]

    def classify(loc_id: int) -> tuple[str, dict[str, Any] | None]:
        if loc_id in rental_out:
            return "rentado", rental_out[loc_id]
        loc = loc_by_id.get(loc_id, {})
        path = f"/{loc.get(lm.f('parent_path')) or ''}"
        wh = next((w for w in warehouses if f"/{m2o_id(w.get(view_f))}/" in path), None)
        name = str(loc.get(lm.f("name")) or "").lower()
        if any(k in name for k in return_kw):
            return "retorno", wh
        if any(k in name for k in maint_kw):
            return "mantenimiento", wh
        return "disponible", wh

    qm = M.STOCK_QUANT
    dom: list[Any] = list(M.RENTAL_PRODUCT_DOMAIN) + [(qm.f("location"), "in", list(loc_by_id) or [0]), (qm.f("quantity"), ">", 0)]
    if ctx.filters.company_id:
        dom.append(("company_id", "=", ctx.filters.company_id))
    gb = [qm.f("location"), qm.f("product")] + ([qm.f("lot")] if qm.has("lot") else [])
    groups = await ctx.odoo.read_group(qm.model, dom, [f"{qm.f('quantity')}:sum"], gb)

    if ctx.branch_ids():  # el inventario no tiene sucursal: con alcance estricto se oculta
        ctx.restrict("Inventario de equipos", "sucursal")
    wanted_wh = set(ctx.warehouse_ids())
    empty = lambda: {s: 0.0 for s, _ in statuses()}  # noqa: E731
    by_wh: dict[str, dict[str, float]] = defaultdict(empty)
    wh_ids: dict[str, int | None] = {}
    by_prod: dict[int, dict[str, float]] = defaultdict(empty)
    prod_names: dict[int, str] = {}
    units: list[dict[str, Any]] = []
    for g in groups:
        loc_id, prod_id = m2o_id(g.get(qm.f("location"))), m2o_id(g.get(qm.f("product")))
        if loc_id is None or prod_id is None:
            continue
        status, wh = classify(loc_id)
        if wanted_wh and (not wh or wh["id"] not in wanted_wh):
            continue
        qty = num(g.get(qm.f("quantity")))
        wh_name = wh["name"] if wh else "Sin almacén"
        wh_ids[wh_name] = wh["id"] if wh else None
        by_wh[wh_name][status] += qty
        by_prod[prod_id][status] += qty
        prod_names[prod_id] = m2o_label(g.get(qm.f("product")))
        lot = g.get(qm.f("lot")) if qm.has("lot") else None
        if status != "rentado":
            units.append({"lot_id": m2o_id(lot), "serie": m2o_label(lot) if lot else "(sin serie)",
                          "product_id": prod_id, "equipo": _short(prod_names[prod_id]),
                          "location_id": loc_id, "ubicacion": str(loc_by_id.get(loc_id, {}).get(lm.f("name")) or ""),
                          "almacen": wh_name, "status": status, "qty": qty})

    cats = await _product_categories(ctx, set(by_prod))
    by_cat: dict[str, dict[str, float]] = defaultdict(empty)
    for pid, vals in by_prod.items():
        for s, v in vals.items():
            by_cat[cats.get(pid, "Sin categoría")][s] += v
    totals = {s: sum(v[s] for v in by_wh.values()) for s, _ in statuses()}
    return {"by_wh": by_wh, "wh_ids": wh_ids, "by_cat": by_cat, "by_prod": by_prod, "prod_names": prod_names,
            "totals": totals, "cats": cats, "units": units}


async def days_in_location(ctx: Ctx, units: list[dict[str, Any]]) -> None:
    """Agrega a cada unidad los días desde su último ingreso a la ubicación actual (stock.move.line)."""
    lots = sorted({u["lot_id"] for u in units if u["lot_id"]})
    if not lots:
        return
    ml = M.MOVE_LINE
    lot_f, dest_f, date_f = ml.f("lot"), ml.f("location_dest"), ml.f("date")
    dom = list(ml.base_domain) + [(lot_f, "in", lots), (dest_f, "in", sorted({u["location_id"] for u in units}))]
    groups = await ctx.odoo.read_group(ml.model, dom, [f"{date_f}:max"], [lot_f, dest_f])
    last: dict[tuple[int | None, int | None], str] = {
        (m2o_id(g.get(lot_f)), m2o_id(g.get(dest_f))): g.get(date_f) for g in groups if g.get(date_f)}
    now = datetime.now(ZoneInfo(ctx.tz))
    for u in units:
        arrived = _parse_dt(last.get((u["lot_id"], u["location_id"])), ctx.tz)
        u["desde"] = arrived.date().isoformat() if arrived else None
        u["dias"] = max(0, (now - arrived).days) if arrived else None


def _age_bucket(days: int) -> str:
    for label, lo, hi in AGE_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return label
    return AGE_BUCKETS[-1][0]


# ------------------------------------------------------------------ contratos
async def contracts_in_period(ctx: Ctx, period: tuple[date, date]) -> list[dict[str, Any]]:
    rc = M.RENTAL_CONTRACT
    start, end, state = rc.f("start"), rc.f("end"), rc.f("state")
    p_start = datetime.combine(period[0], datetime.min.time(), ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    p_end = datetime.combine(period[1] + timedelta(days=1), datetime.min.time(), ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    dom = await build_domain(ctx, rc, "Contratos de renta", extra=[
        (start, "<", p_end.strftime(fmt)), (end, ">=", p_start.strftime(fmt)),
        (state, "not in", M.RENTAL_CONTRACT_EXCLUDED_STATES)])
    fields = [start, end, rc.f("qty"), rc.f("product"), state]
    for opt in ("closed", "price"):
        if rc.has(opt):
            fields.append(rc.f(opt))
    return await ctx.odoo.search_read(rc.model, dom, fields)


def _bounds(period: tuple[date, date], tz: str) -> tuple[datetime, datetime]:
    return (datetime.combine(period[0], datetime.min.time(), ZoneInfo(tz)),
            datetime.combine(period[1] + timedelta(days=1), datetime.min.time(), ZoneInfo(tz)))


def unit_days(contracts: list[dict[str, Any]], period: tuple[date, date], tz: str) -> dict[int, float]:
    """Unidades-día rentadas por producto dentro del periodo."""
    rc = M.RENTAL_CONTRACT
    p_start, p_end = _bounds(period, tz)
    out: dict[int, float] = defaultdict(float)
    for c in contracts:
        s, e = _parse_dt(c.get(rc.f("start")), tz), _parse_dt(c.get(rc.f("end")), tz)
        closed = _parse_dt(c.get(rc.fields.get("closed") or ""), tz) if rc.has("closed") else None
        if closed and e and closed < e:
            e = closed
        pid = m2o_id(c.get(rc.f("product")))
        if not s or not e or pid is None:
            continue
        overlap = (min(e, p_end) - max(s, p_start)).total_seconds() / 86400
        if overlap > 0:
            out[pid] += overlap * (num(c.get(rc.f("qty"))) or 1)
    return out


def revenue_by_product(contracts: list[dict[str, Any]], period: tuple[date, date], tz: str) -> dict[int, float]:
    """Importe de contratos (sin IVA) prorrateado por los días del contrato que caen en el periodo."""
    rc = M.RENTAL_CONTRACT
    if not rc.has("price"):
        return {}
    p_start, p_end = _bounds(period, tz)
    out: dict[int, float] = defaultdict(float)
    for c in contracts:
        s, e = _parse_dt(c.get(rc.f("start")), tz), _parse_dt(c.get(rc.f("end")), tz)
        pid = m2o_id(c.get(rc.f("product")))
        if not s or not e or pid is None or e <= s:
            continue
        overlap = (min(e, p_end) - max(s, p_start)).total_seconds()
        if overlap > 0:
            out[pid] += num(c.get(rc.f("price"))) * overlap / (e - s).total_seconds()
    return out


async def expiring(ctx: Ctx, days: int = 30) -> tuple[dict[int, int], list[dict[str, Any]]]:
    """Conteo exacto por ventana (search_count) + detalle (hasta 2000) para la tabla y los retornos esperados."""
    rc = M.RENTAL_CONTRACT
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"

    async def dom_for(n: int) -> list[Any]:
        return await build_domain(ctx, rc, "Rentas por vencer", extra=[
            (rc.f("end"), ">=", now.strftime(fmt)), (rc.f("end"), "<=", (now + timedelta(days=n)).strftime(fmt)),
            (rc.f("state"), "in", M.RENTAL_CONTRACT_ACTIVE_STATES)])

    counts = {n: await ctx.odoo.search_count(rc.model, await dom_for(n)) for n in EXPIRING_WINDOWS}
    fields = ["name", rc.f("end"), rc.f("product"), rc.f("partner"), rc.f("order"), rc.f("qty")]
    if rc.has("location_dest"):
        fields.append(rc.f("location_dest"))
    rows = await ctx.odoo.search_read(rc.model, await dom_for(days), fields, order=f"{rc.f('end')} asc", limit=2000)
    return counts, rows


async def repair_cost_by_product(ctx: Ctx, period: tuple[date, date]) -> dict[int, float]:
    ro = M.REPAIR_ORDER
    if await ensure_model(ctx, ro) or not ro.has("product"):
        return {}
    dom = await build_domain(ctx, ro, "Órdenes de reparación", period=period)
    groups = await ctx.odoo.read_group(ro.model, dom, [f"{ro.f('amount')}:sum"], [ro.f("product")])
    return {pid: num(g.get(ro.f("amount"))) for g in groups if (pid := m2o_id(g.get(ro.f("product"))))}


async def rental_revenue_by_warehouse(ctx: Ctx, period: tuple[date, date]) -> dict[int, float]:
    sr = M.SALE_REPORT
    if not sr.warehouse_field:
        return {}
    dom = await build_domain(ctx, sr, "Ingresos de renta", period=period, extra=list(M.RENTAL_ORDER_FILTER))
    groups = await ctx.odoo.read_group(sr.model, dom, [f"{sr.f('amount')}:sum"], [sr.warehouse_field])
    return {wid: num(g.get(sr.f("amount"))) for g in groups if (wid := m2o_id(g.get(sr.warehouse_field)))}


# ------------------------------------------------------------------ historial
async def _record_history(ctx: Ctx, snap: dict[str, Any]) -> None:
    f = ctx.filters
    if not ctx.history or not ctx.history.enabled or f.company_id or ctx.warehouse_ids() or ctx.branch_ids():
        return
    if f.date_to < date.today():
        return
    rows = [(snap["wh_ids"].get(wh) or 0, wh, s, v[s]) for wh, v in snap["by_wh"].items() for s, _ in statuses()]
    await ctx.history.record_fleet(datetime.now(ZoneInfo(ctx.tz)).date(), rows)


async def _history_chart(ctx: Ctx, period: tuple[date, date]) -> Chart | None:
    if not ctx.history or not ctx.history.enabled:
        return None
    start = min(period[0], period[1] - timedelta(days=89))
    if ctx.branch_ids() and not ctx.warehouse_ids():
        return None  # el historial es por almacén, no por sucursal
    series = await ctx.history.fleet_series(start, period[1], ctx.warehouse_ids() or None)
    if len(series) < 2:
        ctx.note("El historial de flota empieza a acumularse hoy: la gráfica de tendencia aparecerá a partir de mañana.")
        return None
    data = []
    for day, vals in sorted(series.items()):
        tot = sum(vals.values())
        data.append({"label": day[5:], "dia": day,
                     "utilizacion": round(vals.get("rentado", 0) / tot * 100, 1) if tot else 0,
                     "cuarentena": round(vals.get("mantenimiento", 0) / tot * 100, 1) if tot else 0})
    return Chart(key="historial_flota", title="Tendencia diaria: % rentado y % en mantenimiento", type="line", unit="%",
                 series=[ChartSeries(key="utilizacion", label="% rentado"), ChartSeries(key="cuarentena", label="% en mantenimiento")],
                 data=data)


# ------------------------------------------------------------------ módulo
async def build(ctx: Ctx) -> Result:
    reason = await ensure_model(ctx, M.RENTAL_CONTRACT)
    if reason:
        raise ModuleUnavailable(f"Módulo de rentas no disponible — {reason}")
    cur, prev = periods(ctx.filters)
    sr = M.SALE_REPORT
    amount = sr.f("amount")

    async def revenue(period: Any) -> float:
        v, _ = await total(ctx, sr, "Ingresos de renta", amount, period, extra=list(M.RENTAL_ORDER_FILTER))
        return v

    (snap, contracts, contracts_prev, exp, rev_cur, rev_prev, rev_month, rev_month_prev, repair_prod,
     rev_wh) = await asyncio.gather(
        safe(ctx, "Inventario de equipos", snapshot(ctx)),
        safe(ctx, "Utilización por contratos", contracts_in_period(ctx, cur)),
        safe(ctx, "Utilización por contratos", contracts_in_period(ctx, prev)) if prev else _none(),
        safe(ctx, "Rentas por vencer", expiring(ctx, 30)),
        safe(ctx, "Ingresos de renta", revenue(cur)),
        safe(ctx, "Ingresos de renta", revenue(prev)) if prev else _none(),
        safe(ctx, "Ingresos de renta", monthly(ctx, sr, "Ingresos de renta", amount, cur, extra=list(M.RENTAL_ORDER_FILTER))),
        safe(ctx, "Ingresos de renta", monthly(ctx, sr, "Ingresos de renta", amount, prev,
                                               extra=list(M.RENTAL_ORDER_FILTER))) if prev else _none(),
        safe(ctx, "Rentabilidad por modelo", repair_cost_by_product(ctx, cur)),
        safe(ctx, "Comparativo por almacén", rental_revenue_by_warehouse(ctx, cur)),
    )

    cards: list[KpiCard] = []
    charts: list[Chart] = []
    tables: list[Table] = []

    if rev_cur is not None:
        cards.append(card("ingresos_renta", "Ingresos de renta (sin IVA)", rev_cur, rev_prev))
    if rev_month is not None:
        charts.append(monthly_chart("ingresos_renta_mes", "Ingresos de renta por mes", rev_month, cur, rev_month_prev, prev))

    # ---------- foto de hoy: estatus, antigüedad en ubicación
    fleet_by_cat: dict[str, float] = {}
    if snap:
        await _record_history(ctx, snap)
        t = snap["totals"]
        fleet = sum(t.values())
        fleet_by_cat = {c: sum(v.values()) for c, v in snap["by_cat"].items()}
        cards += [
            card("unidades_total", "Unidades en flota", fleet, unit="count"),
            card("unidades_disponibles", "Disponibles para renta", t["disponible"], unit="count"),
            *([card("unidades_retorno", "En retorno / inspección", t["retorno"], unit="count", higher_is_better=False,
                    hint="Regresaron de renta y aún no se pueden rentar")] if "retorno" in t else []),
            card("unidades_rentadas", "Rentadas", t["rentado"], unit="count"),
            card("unidades_mantenimiento", "En mantenimiento", t["mantenimiento"], unit="count",
                 higher_is_better=False),
            card("utilizacion_actual", "Utilización actual", (t["rentado"] / fleet * 100) if fleet else None, unit="%",
                 hint="Rentadas / total de unidades hoy"),
        ]
        charts.append(Chart(
            key="unidades_sucursal", title="Unidades por almacén y estatus (hoy) — clic para filtrar", type="stacked_bar",
            unit="count", drill="warehouse_id",
            series=[ChartSeries(key=s, label=l) for s, l in statuses()],
            data=[{"label": wh, "_id": snap["wh_ids"].get(wh), **{s: round(v[s], 2) for s, _ in statuses()}}
                  for wh, v in sorted(snap["by_wh"].items(), key=lambda kv: -sum(kv[1].values()))],
        ))
        units = snap["units"]
        for u in units:
            u["dias"], u["desde"] = None, None
        await safe(ctx, "Días en ubicación", days_in_location(ctx, units))
        _fleet_age_sections(ctx, units, cards, charts, tables)

    # ---------- utilización y rentabilidad por modelo (contratos del periodo)
    ud: dict[int, float] = {}
    if contracts is not None:
        days = (cur[1] - cur[0]).days + 1
        ud = unit_days(contracts, cur, ctx.tz)
        cats = (snap or {}).get("cats", {})
        missing = {pid for pid in ud if pid not in cats}
        if missing:
            cats = {**cats, **await _product_categories(ctx, missing)}
        ud_cat: dict[str, float] = defaultdict(float)
        for pid, v in ud.items():
            ud_cat[cats.get(pid, "Sin categoría")] += v
        rows = []
        for cat in sorted(set(ud_cat) | set(fleet_by_cat)):
            fleet_c = fleet_by_cat.get(cat, 0)
            util = (ud_cat[cat] / (fleet_c * days) * 100) if fleet_c else None
            rows.append({"label": cat, "dias_renta": round(ud_cat[cat], 1), "flota": fleet_c,
                         "utilizacion": round(min(util, 999), 1) if util is not None else None})
        rows.sort(key=lambda r: r["utilizacion"] or 0, reverse=True)
        total_fleet = sum(fleet_by_cat.values())
        util_period = (sum(ud.values()) / (total_fleet * days) * 100) if total_fleet else None
        util_prev = None
        if contracts_prev is not None and prev and total_fleet:
            prev_days = (prev[1] - prev[0]).days + 1
            util_prev = sum(unit_days(contracts_prev, prev, ctx.tz).values()) / (total_fleet * prev_days) * 100
        cards.append(card("utilizacion_periodo", "Utilización del periodo", util_period, util_prev, unit="%",
                          hint="Unidades-día rentadas / (flota actual × días)"))
        cards.append(card("contratos_periodo", "Contratos activos en el periodo", len(contracts),
                          len(contracts_prev) if contracts_prev is not None else None, unit="count"))
        charts.append(Chart(key="utilizacion_categoria", title="Utilización del periodo por categoría", type="hbar", unit="%",
                            series=[ChartSeries(key="valor", label="Utilización")],
                            data=[{"label": r["label"], "valor": r["utilizacion"] or 0} for r in rows[:15]]))
        tables.append(Table(
            key="utilizacion_categoria_tabla", title="Utilización por categoría (periodo)",
            columns=[Column(key="label", label="Categoría"), Column(key="flota", label="Unidades (hoy)", type="number"),
                     Column(key="dias_renta", label="Unidades-día rentadas", type="number"),
                     Column(key="utilizacion", label="Utilización", type="percent")],
            rows=rows))
        if not fleet_by_cat:
            ctx.note("Sin inventario de equipos rentables: la utilización por categoría no se puede calcular.")
        if snap:
            tables.append(_profitability_table(snap, ud, revenue_by_product(contracts, cur, ctx.tz), repair_prod, days))

    # ---------- rentas por vencer y retornos esperados
    returns_by_wh: dict[str, int] = defaultdict(int)
    if exp is not None:
        rc = M.RENTAL_CONTRACT
        now = datetime.now(ZoneInfo(ctx.tz))
        exp_counts, exp_rows = exp
        loc_to_wh = await _rental_out_to_warehouse(ctx)
        weeks: dict[str, dict[str, float]] = defaultdict(lambda: {w[0]: 0.0 for w in RETURN_WEEKS})
        wh_id_by_name: dict[str, int | None] = {}
        rows = []
        for c in exp_rows:
            end = _parse_dt(c.get(rc.f("end")), ctx.tz)
            if not end:
                continue
            days_left = max(0, (end - now).days)
            wh = loc_to_wh.get(m2o_id(c.get(rc.fields.get("location_dest") or "")) or 0)
            wh_name = wh["name"] if wh else "Sin almacén"
            wh_id_by_name[wh_name] = wh["id"] if wh else None
            qty = num(c.get(rc.f("qty"))) or 1
            week = next(w[0] for w in RETURN_WEEKS if days_left <= w[2])
            weeks[wh_name][week] += qty
            returns_by_wh[wh_name] += int(qty)
            rows.append({"_id": c["id"], "contrato": c.get("name") or "", "pedido": m2o_label(c.get(rc.f("order"))),
                         "cliente": m2o_label(c.get(rc.f("partner"))), "equipo": _short(m2o_label(c.get(rc.f("product")))),
                         "almacen": wh_name, "fin": end.date().isoformat(), "dias": days_left})
        for n in EXPIRING_WINDOWS:
            cards.append(card(f"vencen_{n}", f"Rentas que vencen en {n} días", exp_counts[n],
                              unit="count", higher_is_better=False, hint="Contratos en curso"))
        if weeks:
            charts.append(Chart(
                key="retornos_esperados", title="Retornos esperados por almacén (próximos 30 días) — clic para filtrar",
                type="stacked_bar", unit="count", drill="warehouse_id",
                series=[ChartSeries(key=w[0], label=w[0]) for w in RETURN_WEEKS],
                data=[{"label": wh, "_id": wh_id_by_name.get(wh), **{k: round(v, 2) for k, v in wk.items()}}
                      for wh, wk in sorted(weeks.items(), key=lambda kv: -sum(kv[1].values()))]))
        title = "Rentas por vencer (próximos 30 días)"
        shown = rows[:300]
        if exp_counts[30] > len(shown):
            title += f" — primeras {len(shown)} de {exp_counts[30]}"
        tables.append(Table(
            key="rentas_por_vencer", title=title, ai_include=False, odoo_model=rc.model,
            columns=[Column(key="contrato", label="Contrato"), Column(key="pedido", label="Pedido"),
                     Column(key="cliente", label="Cliente", entity="cliente"), Column(key="equipo", label="Equipo"),
                     Column(key="almacen", label="Almacén"),
                     Column(key="fin", label="Fin", type="date"), Column(key="dias", label="Días restantes", type="number")],
            rows=shown))

    # ---------- comparativo por almacén
    if snap:
        tables.insert(0, _warehouse_table(snap, rev_wh or {}, returns_by_wh))
    history_chart = await _history_chart(ctx, cur)  # después de guardar la foto de hoy
    if history_chart:
        charts.append(history_chart)
    return cards, charts, tables


def _fleet_age_sections(ctx: Ctx, units: list[dict[str, Any]], cards: list[KpiCard], charts: list[Chart],
                        tables: list[Table]) -> None:
    """Antigüedad en mantenimiento y equipos ociosos (días en la ubicación actual)."""
    quarantine = [u for u in units if u["status"] == "mantenimiento"]
    available = [u for u in units if u["status"] == "disponible"]
    known_q = [u["dias"] for u in quarantine if u.get("dias") is not None]
    idle = [u for u in available if (u.get("dias") or 0) >= M.IDLE_DAYS]
    cards.append(card("cuarentena_dias_prom", "Días promedio en mantenimiento",
                      sum(known_q) / len(known_q) if known_q else None, unit="days", higher_is_better=False,
                      hint=f"{sum(1 for d in known_q if d > 30)} unidades llevan más de 30 días"))
    cards.append(card("equipos_ociosos", f"Equipos ociosos (+{M.IDLE_DAYS} días)", sum(u["qty"] for u in idle),
                      unit="count", higher_is_better=False,
                      hint=f"Disponibles sin moverse en {M.IDLE_DAYS} días o más"))

    buckets = {label: {"disponible": 0.0, "mantenimiento": 0.0} for label, _, _ in AGE_BUCKETS}
    for u in quarantine + available:
        if u.get("dias") is not None:
            buckets[_age_bucket(u["dias"])][u["status"]] += u["qty"]
    charts.append(Chart(
        key="antiguedad_ubicacion", title="Días en la ubicación actual: disponibles vs en mantenimiento", type="bar",
        unit="count", series=[ChartSeries(key="disponible", label="Disponibles"),
                              ChartSeries(key="mantenimiento", label="En mantenimiento")],
        data=[{"label": label, **vals} for label, vals in buckets.items()]))

    cols = [Column(key="serie", label="Serie"), Column(key="equipo", label="Equipo"), Column(key="almacen", label="Almacén"),
            Column(key="ubicacion", label="Ubicación"), Column(key="desde", label="Desde", type="date"),
            Column(key="dias", label="Días", type="days")]

    def detail(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = sorted(items, key=lambda u: (u.get("dias") is None, -(u.get("dias") or 0)))[:DETAIL_LIMIT]
        return [{"_id": u["lot_id"], **{c.key: u.get(c.key) for c in cols}} for u in items]

    tables.append(Table(key="cuarentena_detalle", title="Equipos en mantenimiento (más antiguos primero)",
                        columns=cols, rows=detail(quarantine), ai_include=False, odoo_model="stock.production.lot"))
    tables.append(Table(key="ociosos_detalle", title=f"Equipos ociosos: disponibles sin moverse +{M.IDLE_DAYS} días",
                        columns=cols, rows=detail(idle), ai_include=False, odoo_model="stock.production.lot"))
    if any(u.get("dias") is None for u in quarantine + available):
        ctx.note("Algunas unidades no tienen movimiento registrado hacia su ubicación actual (o no tienen serie): "
                 "se excluyen de los días en ubicación.")


def _profitability_table(snap: dict[str, Any], ud: dict[int, float], revenue: dict[int, float],
                         repair: dict[int, float] | None, days: int) -> Table:
    rows = []
    for pid, vals in snap["by_prod"].items():
        units = sum(vals.values())
        if not units:
            continue
        rev = revenue.get(pid, 0.0)
        cost = (repair or {}).get(pid, 0.0)
        rows.append({
            "_id": pid, "modelo": _short(snap["prod_names"].get(pid, "")), "unidades": units,
            "utilizacion": round(min(ud.get(pid, 0.0) / (units * days) * 100, 999), 1),
            "ingreso": round(rev, 2), "ingreso_unidad": round(rev / units, 2),
            "costo_reparacion": round(cost, 2),
            "costo_pct": round(cost / rev * 100, 1) if rev else None,
        })
    rows.sort(key=lambda r: -r["unidades"])
    return Table(
        key="rentabilidad_modelo", title="Rentabilidad por modelo de equipo (periodo)", odoo_model="product.product",
        columns=[Column(key="modelo", label="Modelo"), Column(key="unidades", label="Unidades", type="number"),
                 Column(key="utilizacion", label="Utilización", type="percent"),
                 Column(key="ingreso", label="Ingreso de contratos", type="money"),
                 Column(key="ingreso_unidad", label="Ingreso por unidad", type="money"),
                 Column(key="costo_reparacion", label="Costo de reparación", type="money"),
                 Column(key="costo_pct", label="Reparación / ingreso", type="percent")],
        rows=rows)


def _warehouse_table(snap: dict[str, Any], rev_wh: dict[int, float], returns_by_wh: dict[str, int]) -> Table:
    rows = []
    for wh, v in snap["by_wh"].items():
        tot = sum(v.values())
        wid = snap["wh_ids"].get(wh)
        rows.append({"_id": wid, "label": wh, **{s: round(v[s], 2) for s, _ in statuses()}, "total": round(tot, 2),
                     "utilizacion": round(v["rentado"] / tot * 100, 1) if tot else None,
                     "cuarentena_pct": round(v["mantenimiento"] / tot * 100, 1) if tot else None,
                     "ingresos": round(rev_wh.get(wid or 0, 0.0), 2), "retornos_30": returns_by_wh.get(wh, 0)})
    rows.sort(key=lambda r: -(r["ingresos"] or 0))
    return Table(
        key="comparativo_almacen", title="Comparativo por almacén", odoo_model="stock.warehouse",
        columns=[Column(key="label", label="Almacén"), Column(key="total", label="Unidades", type="number"),
                 Column(key="rentado", label="Rentadas", type="number"),
                 Column(key="disponible", label="Disponibles", type="number"),
                 *([Column(key="retorno", label="En retorno", type="number")] if M.RETURN_LOCATION_KEYWORDS else []),
                 Column(key="mantenimiento", label="Mantenimiento", type="number"),
                 Column(key="utilizacion", label="Utilización", type="percent"),
                 Column(key="cuarentena_pct", label="% mantenimiento", type="percent"),
                 Column(key="ingresos", label="Ingresos renta (periodo)", type="money"),
                 Column(key="retornos_30", label="Retornos 30 d", type="number")],
        rows=rows)


async def _rental_out_to_warehouse(ctx: Ctx) -> dict[int, dict[str, Any]]:
    ro_f = M.WAREHOUSE.fields.get("rental_out_location") or ""
    out: dict[int, dict[str, Any]] = {}
    for w in await ctx.warehouses():
        loc = m2o_id(w.get(ro_f)) if ro_f else None
        if loc:
            out[loc] = w
    return out


async def _none() -> None:
    return None
