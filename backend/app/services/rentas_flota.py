"""Rentas: unidades contadas con el estado de cada equipo en Odoo › Flota (la misma tabla del Histórico de flota).

Así «Rentas e inventario» y «Histórico de flota» dan las mismas cifras. El inventario (stock.quant) se usa solo para
la conciliación: series sin equipo en Flota, equipos sin existencia, series duplicadas o estados que no coinciden con
la ubicación física.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .. import odoo_mapping as M
from ..config import Settings, runtime_settings
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, Table
from .common import Ctx, card, m2o_id, m2o_label, num
from .fleet_history import STATE_GROUPS, FleetStore, get_fleet_store, state_label
from .historico import _company_warehouses, resolve_states

GROUPS = [("disponibles", "Disponible", "Disponibles para renta"), ("por_entregar", "Entrega", "Por entregar"),
          ("rentadas", "Rentado", "Rentadas"), ("mantenimiento", "Mantenimiento", "En mantenimiento"),
          ("por_recolectar", "Recoleccion", "Por recolectar"), ("subasta", "Subasta", "En subasta")]
GROUP_KEY = {label: key for key, label, _ in GROUPS}
OTHER = "otros"
AGE_BUCKETS = [("0-30 días", 0, 30), ("31-60 días", 31, 60), ("61-90 días", 61, 90), ("91-180 días", 91, 180),
               ("+180 días", 181, None)]
DETAIL_LIMIT = 500
RECON_LIMIT = 1000
TREND_DAYS = 90


@dataclass
class Fleet:
    store: FleetStore
    today: date
    last_sync: str
    states: dict[int, str]
    occ: set[int]
    excl: set[int]
    wh_ids: list[int] | None
    units: list[dict[str, Any]] = field(default_factory=list)

    def group(self, state_id: int | None) -> str:
        name = self.states.get(state_id or 0, "")
        return GROUP_KEY.get(STATE_GROUPS.get(name.lower(), ""), OTHER)

    def in_fleet(self, state_id: int | None) -> bool:
        return state_id not in self.excl


async def load(ctx: Ctx) -> Fleet | None:
    """Flota de hoy desde el histórico local; ``None`` si el histórico aún no existe (se usa el inventario)."""
    settings: Settings = ctx.settings or runtime_settings()
    store = get_fleet_store(ctx.history.path if ctx.history else settings.HISTORY_DB_PATH)
    if not store.enabled:
        return None
    meta = await asyncio.to_thread(store.meta)
    if not meta.get("last_sync") or not meta.get("built_until"):
        return None
    states = dict(await asyncio.to_thread(store.states))
    occ, excl = resolve_states(list(states.items()), settings)
    if ctx.branch_ids():
        ctx.restrict("Unidades de flota", "sucursal")
    fleet = Fleet(store, date.fromisoformat(meta["built_until"]), meta["last_sync"], states, occ, excl,
                  ctx.warehouse_ids() or await _company_warehouses(ctx))
    fleet.units = await asyncio.to_thread(store.units_on, fleet.today, fleet.wh_ids)
    for u in fleet.units:
        u["grupo"] = fleet.group(u["state_id"])
        u["estado"] = state_label(states.get(u["state_id"] or 0, ""))
        u["dias"] = (fleet.today - date.fromisoformat(u["since"])).days if u.get("since") else None
    return fleet


def _occupancy(rows: list[tuple[Any, int, int]], fl: Fleet) -> dict[Any, tuple[float, float]]:
    """{llave: (unidades-día en flota, unidades-día ocupadas)}."""
    out: dict[Any, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for key, st, n in rows:
        if fl.in_fleet(st):
            out[key][0] += n
            if st in fl.occ:
                out[key][1] += n
    return {k: (v[0], v[1]) for k, v in out.items()}


def _pct(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


async def sections(ctx: Ctx, fl: Fleet, cur: tuple[date, date], prev: tuple[date, date] | None,
                   cards: list[KpiCard], charts: list[Chart], tables: list[Table]) -> dict[str, Any]:
    """Tarjetas, gráficas y tablas de unidades. Devuelve datos para la rentabilidad y el comparativo por almacén."""
    units = [u for u in fl.units if fl.in_fleet(u["state_id"])]
    by_group = Counter(u["grupo"] for u in units)
    total_units = len(units)
    occupied = sum(1 for u in units if u["state_id"] in fl.occ)
    occ_names = ", ".join(state_label(fl.states[s]) for s in sorted(fl.occ) if s in fl.states) or "ninguno"
    cards.append(card("unidades_total", "Unidades en flota", total_units, unit="count",
                      hint="Equipos de renta en Odoo › Flota (mismo dato que el Histórico de flota)"))
    for key, label, title in GROUPS:
        if by_group.get(key) or key in ("disponibles", "rentadas", "mantenimiento"):
            cards.append(card(f"unidades_{key}", title, by_group.get(key, 0), unit="count",
                              higher_is_better=key not in ("mantenimiento", "subasta", "por_recolectar")))
    cards.append(card("utilizacion_actual", "Utilización actual", _pct(occupied, total_units), unit="%",
                      hint=f"({occ_names}) / unidades en flota"))

    # ---------- unidades por almacén (hoy)
    by_wh: dict[str, Counter] = defaultdict(Counter)
    wh_id: dict[str, int | None] = {}
    for u in units:
        by_wh[u["warehouse"]][u["grupo"]] += 1
        wh_id[u["warehouse"]] = u["warehouse_id"] or None
    present = [(k, label) for k, label, _ in GROUPS if by_group.get(k)] + ([(OTHER, "Otros")] if by_group.get(OTHER) else [])
    charts.append(Chart(
        key="unidades_sucursal", title="Unidades por almacén y estado (hoy) — clic para filtrar", type="stacked_bar",
        unit="count", drill="warehouse_id", series=[ChartSeries(key=k, label=l) for k, l in present],
        data=[{"label": wh, "_id": wh_id.get(wh), **{k: c.get(k, 0) for k, _ in present}}
              for wh, c in sorted(by_wh.items(), key=lambda kv: -sum(kv[1].values()))]))

    # ---------- días en el estado actual: mantenimiento y equipos ociosos
    maint = [u for u in units if u["grupo"] == "mantenimiento"]
    avail = [u for u in units if u["grupo"] == "disponibles"]
    known = [u["dias"] for u in maint if u["dias"] is not None]
    idle = [u for u in avail if (u["dias"] or 0) >= M.IDLE_DAYS]
    cards.append(card("mantenimiento_dias_prom", "Días promedio en mantenimiento",
                      sum(known) / len(known) if known else None, unit="days", higher_is_better=False,
                      hint=f"{sum(1 for d in known if d > 30)} unidades llevan más de 30 días"))
    cards.append(card("equipos_ociosos", f"Equipos ociosos (+{M.IDLE_DAYS} días)", len(idle), unit="count",
                      higher_is_better=False, hint=f"Disponibles sin cambiar de estado en {M.IDLE_DAYS} días o más"))
    buckets = {label: {"disponibles": 0, "mantenimiento": 0} for label, _, _ in AGE_BUCKETS}
    for u in maint + avail:
        if u["dias"] is not None:
            label = next(l for l, lo, hi in AGE_BUCKETS if u["dias"] >= lo and (hi is None or u["dias"] <= hi))
            buckets[label][u["grupo"]] += 1
    charts.append(Chart(
        key="antiguedad_ubicacion", title="Días en el estado actual: disponibles vs en mantenimiento", type="bar",
        unit="count", ordered=True, series=[ChartSeries(key="disponibles", label="Disponibles"),
                              ChartSeries(key="mantenimiento", label="En mantenimiento")],
        data=[{"label": label, **vals} for label, vals in buckets.items()]))
    cols = [Column(key="placa", label="Placa"), Column(key="serie", label="Serie"), Column(key="equipo", label="Equipo"),
            Column(key="almacen", label="Almacén"), Column(key="estado", label="Estado"),
            Column(key="desde", label="Desde", type="date"), Column(key="dias", label="Días", type="days")]

    def detail(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = sorted(items, key=lambda u: -(u["dias"] or 0))[:DETAIL_LIMIT]
        return [{"_id": u["id"], "placa": u["plate"] or u["name"], "serie": u["serie"], "equipo": u["product"],
                 "almacen": u["warehouse"], "estado": u["estado"], "desde": u["since"], "dias": u["dias"]} for u in items]

    tables.append(Table(key="mantenimiento_detalle", title="Equipos en mantenimiento (más antiguos primero)",
                        columns=cols, rows=detail(maint), ai_include=False, odoo_model="fleet.vehicle"))
    tables.append(Table(key="ociosos_detalle", title=f"Equipos ociosos: disponibles sin cambiar de estado +{M.IDLE_DAYS} días",
                        columns=cols, rows=detail(idle), ai_include=False, odoo_model="fleet.vehicle"))

    # ---------- utilización del periodo (promedio diario, igual que el Histórico de flota)
    span = (cur[0], min(cur[1], fl.today))
    occ_cur = _occupancy(await asyncio.to_thread(fl.store.group_days, span[0], span[1], fl.wh_ids, "familia"), fl) \
        if span[0] <= span[1] else {}
    util_prev = None
    if prev and prev[0] <= fl.today:
        p = _occupancy(await asyncio.to_thread(fl.store.group_days, prev[0], min(prev[1], fl.today), fl.wh_ids,
                                               "familia"), fl)
        util_prev = _pct(sum(v[1] for v in p.values()), sum(v[0] for v in p.values()))
    cards.append(card("utilizacion_periodo", "Utilización del periodo",
                      _pct(sum(v[1] for v in occ_cur.values()), sum(v[0] for v in occ_cur.values())), util_prev,
                      unit="%", hint="Promedio diario de unidades ocupadas / flota (al cierre de cada día)"))
    fleet_by_fam = Counter(u["familia"] or "Sin familia" for u in units)
    rows = [{"label": fam or "Sin familia", "flota": fleet_by_fam.get(fam or "Sin familia", 0),
             "dias_renta": round(o, 1), "utilizacion": _pct(o, f)} for fam, (f, o) in occ_cur.items()]
    rows.sort(key=lambda r: r["utilizacion"] or 0, reverse=True)
    charts.append(Chart(key="utilizacion_categoria", title="Utilización del periodo por familia", type="hbar", unit="%",
                        series=[ChartSeries(key="valor", label="Utilización")],
                        data=[{"label": r["label"], "valor": r["utilizacion"] or 0} for r in rows[:15]]))
    tables.append(Table(
        key="utilizacion_categoria_tabla", title="Utilización por familia (periodo)",
        columns=[Column(key="label", label="Familia"), Column(key="flota", label="Unidades (hoy)", type="number"),
                 Column(key="dias_renta", label="Unidades-día ocupadas", type="number"),
                 Column(key="utilizacion", label="Utilización", type="percent")],
        rows=rows))

    # ---------- tendencia diaria (últimos 90 días)
    start = min(span[0], span[1] - timedelta(days=TREND_DAYS - 1)) if span[0] <= span[1] else fl.today
    per_day: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for day, _, st, n in await asyncio.to_thread(fl.store.counts, start, span[1], fl.wh_ids):
        if fl.in_fleet(st):
            per_day[day][0] += n
            per_day[day][1] += n if st in fl.occ else 0
            per_day[day][2] += n if fl.group(st) == "mantenimiento" else 0
    if len(per_day) > 1:
        charts.append(Chart(
            key="historial_flota", title="Tendencia diaria: % ocupado y % en mantenimiento (cierre 23:00)", type="line",
            unit="%", series=[ChartSeries(key="utilizacion", label="% ocupado"),
                              ChartSeries(key="mantenimiento", label="% en mantenimiento")],
            data=[{"label": d[5:], "dia": d, "utilizacion": _pct(v[1], v[0]) or 0, "mantenimiento": _pct(v[2], v[0]) or 0}
                  for d, v in sorted(per_day.items())]))

    by_prod = Counter(u["product_id"] for u in units if u["product_id"])
    prod_names = {u["product_id"]: u["product"] for u in units if u["product_id"]}
    prod_occ = _occupancy(await asyncio.to_thread(fl.store.group_days, span[0], span[1], fl.wh_ids, "product_id"), fl) \
        if span[0] <= span[1] else {}
    ctx.note(f"Unidades: estado de cada equipo en Odoo › Flota al cierre del día (23:00, hora de México); hoy, "
             f"al momento de la última sincronización ({fl.last_sync[:16].replace('T', ' ')}). Son las mismas cifras "
             f"del Histórico de flota.")
    return {"by_wh": by_wh, "wh_id": wh_id, "by_prod": by_prod, "prod_names": prod_names, "prod_occ": prod_occ}


def profitability_table(data: dict[str, Any], revenue: dict[int, float], repair: dict[int, float] | None) -> Table:
    rows = []
    for pid, n in data["by_prod"].items():
        rev, cost = revenue.get(pid, 0.0), (repair or {}).get(pid, 0.0)
        f, o = data["prod_occ"].get(pid, (0.0, 0.0))
        rows.append({"_id": pid, "modelo": data["prod_names"].get(pid, ""), "unidades": n, "utilizacion": _pct(o, f),
                     "ingreso": round(rev, 2), "ingreso_unidad": round(rev / n, 2), "costo_reparacion": round(cost, 2),
                     "costo_pct": _pct(cost, rev)})
    rows.sort(key=lambda r: -r["unidades"])
    return Table(
        key="rentabilidad_modelo", title="Rentabilidad por modelo de equipo (periodo)", odoo_model="product.product",
        columns=[Column(key="modelo", label="Modelo"), Column(key="unidades", label="Unidades (hoy)", type="number"),
                 Column(key="utilizacion", label="Utilización", type="percent"),
                 Column(key="ingreso", label="Ingreso de contratos", type="money"),
                 Column(key="ingreso_unidad", label="Ingreso por unidad", type="money"),
                 Column(key="costo_reparacion", label="Costo de reparación", type="money"),
                 Column(key="costo_pct", label="Reparación / ingreso", type="percent")],
        rows=rows)


def warehouse_table(data: dict[str, Any], fl: Fleet, rev_wh: dict[int, float], returns_by_wh: dict[str, int]) -> Table:
    rows = []
    for wh, c in data["by_wh"].items():
        tot = sum(c.values())
        wid = data["wh_id"].get(wh)
        occ = sum(1 for u in fl.units if u["warehouse"] == wh and fl.in_fleet(u["state_id"]) and u["state_id"] in fl.occ)
        rows.append({"_id": wid, "label": wh, "total": tot, **{k: c.get(k, 0) for k, _, _ in GROUPS},
                     "utilizacion": _pct(occ, tot), "mantenimiento_pct": _pct(c.get("mantenimiento", 0), tot),
                     "ingresos": round(rev_wh.get(wid or 0, 0.0), 2), "retornos_30": returns_by_wh.get(wh, 0)})
    rows.sort(key=lambda r: -(r["ingresos"] or 0))
    return Table(
        key="comparativo_almacen", title="Comparativo por almacén", odoo_model="stock.warehouse",
        columns=[Column(key="label", label="Almacén"), Column(key="total", label="Unidades", type="number"),
                 *[Column(key=k, label=label, type="number") for k, label, _ in GROUPS],
                 Column(key="utilizacion", label="Utilización", type="percent"),
                 Column(key="mantenimiento_pct", label="% mantenimiento", type="percent"),
                 Column(key="ingresos", label="Ingresos renta (periodo)", type="money"),
                 Column(key="retornos_30", label="Retornos 30 d", type="number")],
        rows=rows)


# ------------------------------------------------------------------ conciliación inventario vs Flota
RECON_TYPES = [
    ("sin_equipo", "Serie en inventario sin equipo en Flota"),
    ("sin_existencia", "Equipo en Flota sin existencia en inventario"),
    ("sin_serie", "Existencia de equipo de renta sin número de serie"),
    ("duplicada", "Serie en más de una ubicación a la vez"),
    ("rentado_en_almacen", "Flota dice rentado, inventario en almacén"),
    ("en_renta_no_rentado", "Inventario en «Rental out», Flota no dice rentado"),
]


async def reconciliation(ctx: Ctx, fl: Fleet, cards: list[KpiCard], tables: list[Table]) -> None:
    warehouses = await ctx.warehouses()
    ro_f = M.WAREHOUSE.fields.get("rental_out_location") or ""
    view_f = M.WAREHOUSE.f("view_location")
    rental_out = {loc: w for w in warehouses if ro_f and (loc := m2o_id(w.get(ro_f)))}
    lm, qm = M.LOCATION, M.STOCK_QUANT
    locs = await ctx.odoo.search_read(lm.model, ["|", (lm.f("usage"), "=", "internal"), ("id", "in", list(rental_out) or [0])],
                                      ["id", lm.f("name"), lm.f("parent_path")])
    loc_by_id = {l["id"]: l for l in locs}

    def wh_of(loc_id: int) -> dict[str, Any] | None:
        if loc_id in rental_out:
            return rental_out[loc_id]
        path = f"/{loc_by_id.get(loc_id, {}).get(lm.f('parent_path')) or ''}"
        return next((w for w in warehouses if f"/{m2o_id(w.get(view_f))}/" in path), None)

    dom: list[Any] = list(M.RENTAL_PRODUCT_DOMAIN) + [(qm.f("location"), "in", list(loc_by_id) or [0]),
                                                       (qm.f("quantity"), ">", 0)]
    if ctx.filters.company_id:
        dom.append(("company_id", "=", ctx.filters.company_id))
    lot_f = qm.f("lot") if qm.has("lot") else None
    quants = await ctx.odoo.search_read(qm.model, dom, [qm.f("location"), qm.f("product"), qm.f("quantity"),
                                                       *([lot_f] if lot_f else [])])
    wanted = set(fl.wh_ids or [])
    vehicles = [v for v in await asyncio.to_thread(fl.store.fleet_vehicles) if v["active"]]
    by_lot = {v["lot_id"]: v for v in vehicles if v["lot_id"]}
    rented_ids = {s for s, n in fl.states.items() if STATE_GROUPS.get(n.lower()) == "Rentado"}
    found: list[dict[str, Any]] = []
    lot_locs: dict[int, list[dict[str, Any]]] = defaultdict(list)

    def add(kind: str, serie: str, equipo: str, estado: str, ubicacion: str, almacen: str, _id: Any) -> None:
        found.append({"_id": _id, "tipo": kind, "serie": serie, "equipo": equipo, "estado": estado,
                      "ubicacion": ubicacion, "almacen": almacen})

    for q in quants:
        loc_id = m2o_id(q.get(qm.f("location"))) or 0
        w = wh_of(loc_id)
        if wanted and (not w or w["id"] not in wanted):
            continue
        lot = q.get(lot_f) if lot_f else None
        info = {"loc_id": loc_id, "loc": m2o_label(q.get(qm.f("location"))), "wh": w["name"] if w else "Sin almacén",
                "product": m2o_label(q.get(qm.f("product"))), "qty": num(q.get(qm.f("quantity")))}
        if not lot:
            add("sin_serie", "(sin serie)", info["product"], "", info["loc"], info["wh"], None)
            continue
        lot_locs[m2o_id(lot) or 0].append({**info, "serie": m2o_label(lot)})

    for lot_id, items in lot_locs.items():
        first = items[0]
        v = by_lot.get(lot_id)
        estado = state_label(fl.states.get(v["state_id"] or 0, "")) if v else ""
        if len(items) > 1:
            add("duplicada", first["serie"], first["product"], estado, " · ".join(i["loc"] for i in items),
                " · ".join(sorted({i["wh"] for i in items})), lot_id)
        if not v:
            add("sin_equipo", first["serie"], first["product"], "", first["loc"], first["wh"], lot_id)
            continue
        in_rent = any(i["loc_id"] in rental_out for i in items)
        if v["state_id"] in rented_ids and not in_rent:
            add("rentado_en_almacen", first["serie"], v["product"], estado, first["loc"], first["wh"], lot_id)
        elif in_rent and v["state_id"] not in rented_ids and fl.group(v["state_id"]) in ("disponibles", "mantenimiento", "subasta"):
            add("en_renta_no_rentado", first["serie"], v["product"], estado, first["loc"], first["wh"], lot_id)

    in_scope = {u["id"] for u in fl.units}
    for v in vehicles:
        if v["rental"] and v["id"] in in_scope and v["lot_id"] not in lot_locs and fl.in_fleet(v["state_id"]):
            wh = next((u["warehouse"] for u in fl.units if u["id"] == v["id"]), "")
            add("sin_existencia", v["serie"] or v["plate"] or v["name"], v["product"],
                state_label(fl.states.get(v["state_id"] or 0, "")), "", wh, v["lot_id"])

    labels = dict(RECON_TYPES)
    counts = Counter(r["tipo"] for r in found)
    cards.append(card("diferencias_inventario", "Diferencias inventario vs Flota", len(found), unit="count",
                      higher_is_better=False, hint="Series a corregir en Odoo para que ambos cuadren"))
    tables.append(Table(
        key="conciliacion_resumen", title="Conciliación inventario vs Flota",
        columns=[Column(key="label", label="Diferencia"), Column(key="n", label="Casos", type="number")],
        rows=[{"label": label, "n": counts.get(k, 0)} for k, label in RECON_TYPES]))
    order = {k: i for i, (k, _) in enumerate(RECON_TYPES)}
    found.sort(key=lambda r: (order[r["tipo"]], r["almacen"], r["serie"]))
    shown = found[:RECON_LIMIT]
    tables.append(Table(
        key="conciliacion_detalle",
        title="Detalle de diferencias inventario vs Flota" + (f" — primeras {len(shown)} de {len(found)}" if len(found) > len(shown) else ""),
        columns=[Column(key="tipo", label="Diferencia"), Column(key="serie", label="Serie"), Column(key="equipo", label="Equipo"),
                 Column(key="estado", label="Estado en Flota"), Column(key="ubicacion", label="Ubicación en inventario"),
                 Column(key="almacen", label="Almacén")],
        rows=[{**r, "tipo": labels[r["tipo"]]} for r in shown], ai_include=False))
