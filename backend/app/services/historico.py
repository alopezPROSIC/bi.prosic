"""Módulo «Histórico de flota»: promedios de ocupación y estados para cualquier periodo.

Lee la tabla diaria reconstruida por ``fleet_history`` (SQLite, no consulta Odoo).
- Ocupación del día = unidades en estados «ocupados» / flota del día.
- Flota del día      = unidades de renta activas en ese día, sin los estados excluidos.
- Promedio del periodo = promedio de los valores diarios (así se calcula al cierre de mes para comisiones).
Los estados ocupados/excluidos se configuran en el panel (FLEET_OCCUPIED_STATES / FLEET_EXCLUDED_STATES).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from ..config import Settings, runtime_settings
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, Table
from .common import Ctx, ModuleUnavailable, card, periods
from .fleet_history import FleetStore, get_fleet_store, state_label

Result = tuple[list[KpiCard], list[Chart], list[Table]]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MAX_MONTH_COLUMNS = 24


def _names(text: str) -> set[str]:
    return {x.strip().lower() for x in (text or "").split(",") if x.strip()}


def resolve_states(states: list[tuple[int, str]], settings: Settings) -> tuple[set[int], set[int]]:
    """(estados ocupados, estados excluidos de la flota). Acepta nombre de Odoo, nombre en español o id."""
    def match(wanted: set[str]) -> set[int]:
        return {sid for sid, name in states
                if name.lower() in wanted or state_label(name).lower() in wanted or str(sid) in wanted}
    return match(_names(settings.FLEET_OCCUPIED_STATES)), match(_names(settings.FLEET_EXCLUDED_STATES))


class Agg:
    """Acumula unidades-día por estado. El promedio se divide entre TODOS los días del periodo (``n``): un día en
    que el almacén no tuvo unidades cuenta como cero, no se omite."""

    def __init__(self, n: int | None = None) -> None:
        self.units: dict[int, float] = defaultdict(float)
        self.days: set[str] = set()
        self.n = n

    def add(self, day: str, state: int, n: int) -> None:
        self.units[state] += n
        self.days.add(day)

    @property
    def _n(self) -> int:
        return self.n if self.n is not None else len(self.days)

    def avg(self, state: int) -> float:
        return self.units.get(state, 0) / self._n if self._n else 0.0

    def fleet(self, excluded: set[int]) -> float:
        return sum(v for s, v in self.units.items() if s not in excluded) / self._n if self._n else 0.0

    def occupied(self, occ: set[int]) -> float:
        return sum(v for s, v in self.units.items() if s in occ) / self._n if self._n else 0.0

    def pct(self, occ: set[int], excluded: set[int]) -> float | None:
        f = self.fleet(excluded)
        return self.occupied(occ) / f * 100 if f else None


def _bucket(day: str, mode: str) -> str:
    if mode == "day":
        return day
    d = date.fromisoformat(day)
    if mode == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    return day[:7]


def _bucket_label(key: str, mode: str) -> str:
    if mode == "month":
        return f"{MONTHS[int(key[5:7]) - 1]} {key[2:4]}"
    d = date.fromisoformat(key)
    return f"{'sem ' if mode == 'week' else ''}{d.day:02d}/{d.month:02d}" + (f"/{d.year % 100:02d}" if mode == "week" else "")


async def _rows(store: FleetStore, period: tuple[date, date], wh_ids: list[int] | None) -> list[tuple[str, int, int, int]]:
    return await asyncio.to_thread(store.counts, period[0], period[1], wh_ids)


async def _company_warehouses(ctx: Ctx) -> list[int] | None:
    if not ctx.filters.company_id:
        return None
    return [w["id"] for w in await ctx.warehouses() if (w.get("company_id") or [None])[0] == ctx.filters.company_id]


async def build(ctx: Ctx) -> Result:
    settings: Settings = ctx.settings or runtime_settings()
    store = get_fleet_store(ctx.history.path if ctx.history else settings.HISTORY_DB_PATH)
    if not store.enabled:
        raise ModuleUnavailable("El histórico de flota requiere HISTORY_DB_PATH (almacenamiento local).")
    meta = await asyncio.to_thread(store.meta)
    if not meta.get("last_sync"):
        raise ModuleUnavailable("El histórico se está generando por primera vez desde Odoo (tarda unos minutos). "
                                "Vuelve a cargar en un momento.")
    states = await asyncio.to_thread(store.states)
    wh_names = await asyncio.to_thread(store.warehouses)
    occ, excl = resolve_states(states, settings)
    if ctx.branch_ids():
        ctx.restrict("Histórico de flota", "sucursal")
    wh_ids = ctx.warehouse_ids() or await _company_warehouses(ctx)
    cur, prev = periods(ctx.filters)
    today = date.fromisoformat(meta["built_until"])
    cur = (cur[0], min(cur[1], today))
    rows = await _rows(store, cur, wh_ids) if cur[0] <= cur[1] else []
    if not rows:
        first = await asyncio.to_thread(store.first_day)
        raise ModuleUnavailable(f"Sin histórico para el periodo elegido. Hay datos del {first} al {meta['built_until']}.")

    first_day = date.fromisoformat(await asyncio.to_thread(store.first_day) or cur[0].isoformat())
    span = (max(cur[0], first_day), cur[1])  # días del periodo que el histórico cubre
    n_days = (span[1] - span[0]).days + 1
    total, by_day, by_wh = Agg(n_days), defaultdict(lambda: Agg(1)), defaultdict(lambda: Agg(n_days))
    by_wh_month: dict[tuple[int, str], Agg] = {}
    for day, wh, st, n in rows:
        total.add(day, st, n)
        by_day[day].add(day, st, n)
        by_wh[wh].add(day, st, n)
        if (wh, day[:7]) not in by_wh_month:
            by_wh_month[(wh, day[:7])] = Agg(_days_in(span, day[:7]))
        by_wh_month[(wh, day[:7])].add(day, st, n)

    prev_total: Agg | None = None
    if prev and prev[1] >= first_day:
        prev_rows = await _rows(store, prev, wh_ids)
        if prev_rows:
            prev_total = Agg((prev[1] - max(prev[0], first_day)).days + 1)
            for day, _, st, n in prev_rows:
                prev_total.add(day, st, n)

    labels = {sid: state_label(name) for sid, name in states}
    present = [sid for sid, _ in states if total.units.get(sid)]
    occ_names = ", ".join(labels[s] for s in present if s in occ) or "ninguno (configúralo en el panel)"

    # ---------------- tarjetas
    pct_prev = prev_total.pct(occ, excl) if prev_total else None
    cards = [
        card("ocupacion_prom", "Ocupación promedio", total.pct(occ, excl), pct_prev, unit="%",
             hint=f"Promedio diario de {occ_names} / flota"),
        card("flota_prom", "Flota de renta promedio", total.fleet(excl), prev_total.fleet(excl) if prev_total else None,
             unit="count", hint="Unidades por día"),
        card("ocupadas_prom", "Unidades ocupadas (promedio)", total.occupied(occ),
             prev_total.occupied(occ) if prev_total else None, unit="count"),
        card("dias_periodo", "Días del periodo", n_days, unit="count",
             hint=f"{_fmt_day(span[0].isoformat())} al {_fmt_day(span[1].isoformat())}"),
    ]
    for sid in present:
        cards.append(card(f"estado_{sid}", f"Promedio diario · {labels[sid]}", total.avg(sid),
                          prev_total.avg(sid) if prev_total else None, unit="count",
                          higher_is_better=sid in occ))

    # ---------------- gráficas (día, semana o mes según el largo del periodo)
    mode = "day" if n_days <= 92 else "week" if n_days <= 400 else "month"
    buckets: dict[str, Agg] = {}
    d = span[0]
    while d <= span[1]:  # cada cubeta divide entre sus días del periodo, aunque alguno no tenga unidades
        k = _bucket(d.isoformat(), mode)
        buckets.setdefault(k, Agg(0)).n += 1  # type: ignore[operator]
        d += timedelta(days=1)
    for day, agg in by_day.items():
        b = buckets[_bucket(day, mode)]
        for st, n in agg.units.items():
            b.add(day, st, int(n))
    unit_word = {"day": "por día", "week": "promedio por semana", "month": "promedio por mes"}[mode]
    series = [ChartSeries(key=f"s{sid}", label=labels[sid]) for sid in present]
    keys = sorted(buckets)
    charts = [
        Chart(key="estados_periodo", title=f"Estado de la flota ({unit_word})", type="stacked_bar", unit="count",
              series=series,
              data=[{"label": _bucket_label(k, mode), **{f"s{sid}": round(buckets[k].avg(sid), 1) for sid in present}}
                    for k in keys]),
        Chart(key="ocupacion_periodo", title=f"% de ocupación ({unit_word})", type="line", unit="%",
              series=[ChartSeries(key="ocupacion", label="Ocupación")],
              data=[{"label": _bucket_label(k, mode), "ocupacion": round(buckets[k].pct(occ, excl) or 0, 1)} for k in keys]),
    ]
    wh_sorted = sorted(by_wh.items(), key=lambda kv: -(kv[1].pct(occ, excl) or 0))
    charts.append(Chart(
        key="ocupacion_almacen", title="Ocupación promedio por almacén — clic para filtrar", type="hbar", unit="%",
        drill="warehouse_id", series=[ChartSeries(key="valor", label="Ocupación")],
        data=[{"label": wh_names.get(w, "Sin almacén"), "_id": w or None, "valor": round(a.pct(occ, excl) or 0, 1)}
              for w, a in wh_sorted if a.fleet(excl)]))

    # ---------------- tablas (todas descargables a CSV desde la vista)
    state_cols = [Column(key=f"s{sid}", label=labels[sid], type="number") for sid in present]
    tables = [
        Table(key="historico_diario", title="Histórico diario de la flota (estado al cierre de cada día)",
              columns=[Column(key="fecha", label="Fecha", type="date"), *state_cols,
                       Column(key="flota", label="Flota", type="number"),
                       Column(key="ocupadas", label="Ocupadas", type="number"),
                       Column(key="ocupacion", label="% ocupación", type="percent")],
              rows=[{"fecha": d, **{f"s{sid}": int(a.units.get(sid, 0)) for sid in present},
                     "flota": int(a.fleet(excl)), "ocupadas": int(a.occupied(occ)),
                     "ocupacion": _r(a.pct(occ, excl))} for d, a in sorted(by_day.items(), reverse=True)]),
        Table(key="promedio_almacen", title="Promedios del periodo por almacén", odoo_model="stock.warehouse",
              columns=[Column(key="label", label="Almacén"), Column(key="flota", label="Flota promedio", type="number"),
                       Column(key="ocupadas", label="Ocupadas promedio", type="number"),
                       Column(key="ocupacion", label="% ocupación", type="percent"),
                       *[Column(key=f"s{sid}", label=labels[sid], type="number") for sid in present]],
              rows=[{"_id": w or None, "label": wh_names.get(w, "Sin almacén"), "flota": _r(a.fleet(excl)),
                     "ocupadas": _r(a.occupied(occ)), "ocupacion": _r(a.pct(occ, excl)),
                     **{f"s{sid}": _r(a.avg(sid)) for sid in present}} for w, a in wh_sorted]),
    ]
    months = sorted({m for _, m in by_wh_month})
    if 1 < len(months) <= MAX_MONTH_COLUMNS:  # con un solo mes repetiría la tabla por almacén
        mcols = [Column(key=f"m{m}", label=_bucket_label(m, "month"), type="percent") for m in months]
        month_rows = []
        for w, _ in wh_sorted:
            month_rows.append({"_id": w or None, "label": wh_names.get(w, "Sin almacén"),
                               **{f"m{m}": _r(by_wh_month[(w, m)].pct(occ, excl)) for m in months if (w, m) in by_wh_month}})
        tables.append(Table(key="ocupacion_almacen_mes", title="% de ocupación promedio por almacén y mes",
                            odoo_model="stock.warehouse",
                            columns=[Column(key="label", label="Almacén"), *mcols], rows=month_rows))

    excl_names = ", ".join(labels[s] for s in sorted(excl) if s in labels)
    ctx.note(f"Ocupación = ({occ_names}) / flota de renta"
             + (f", sin contar {excl_names}" if excl_names else "") + ". Se cambia en Administración → Configuración.")
    ctx.note("Reconstruido con el historial de cambios de estado de cada máquina en Odoo (Flota) y sus movimientos "
             f"de inventario; estado al cierre de cada día. Datos desde {_fmt_day(await asyncio.to_thread(store.first_day) or '')}; "
             f"última sincronización {meta['last_sync'][:16].replace('T', ' ')}. Hoy cuenta con el estado actual.")
    if cur[1] < ctx.filters.date_to:
        ctx.note("Las fechas futuras no tienen datos todavía.")
    return cards, charts, tables


def _days_in(span: tuple[date, date], month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    start = max(span[0], date(y, m, 1))
    end = min(span[1], (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)))
    return max((end - start).days + 1, 0)


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def _fmt_day(d: str) -> str:
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if len(d) >= 10 else d
