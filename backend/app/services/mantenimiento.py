"""KPIs de Mantenimiento (maintenance.request, maintenance.equipment, repair.order)."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, ChartSeries, KpiCard, Table
from .common import (
    Ctx,
    ModuleUnavailable,
    bar_chart,
    build_domain,
    card,
    date_domain,
    ensure_model,
    group_month_key,
    m2o_label,
    monthly,
    monthly_chart,
    periods,
    safe,
    top_groups,
)

Result = tuple[list[KpiCard], list[Chart], list[Table]]
TYPE_LABELS = {"preventive": "Preventivo", "corrective": "Correctivo"}


async def _count(ctx: Ctx, period: Any, extra: list[Any]) -> int:
    mr = M.MAINT_REQUEST
    dom = await build_domain(ctx, mr, "Peticiones de mantenimiento", period=period,
                             extra=list(M.MAINT_EXCLUDE_DOMAIN) + extra)
    return await ctx.odoo.search_count(mr.model, dom)


async def _resolution(ctx: Ctx, period: Any) -> tuple[float | None, dict[str, float]]:
    """Días promedio entre solicitud y cierre de las peticiones CERRADAS en el periodo."""
    mr = M.MAINT_REQUEST
    req, close, typ = mr.f("request_date"), mr.f("close_date"), mr.f("type")
    dom = await build_domain(ctx, mr, "Tiempo de resolución", extra=list(M.MAINT_CLOSED_DOMAIN) + [(close, "!=", False)])
    dom += await date_domain(ctx, mr, *period, field_name=close)
    rows = await ctx.odoo.search_read(mr.model, dom, [req, close, typ])
    per_type: dict[str, list[float]] = {}
    all_days: list[float] = []
    for r in rows:
        if not r.get(req) or not r.get(close):
            continue
        d = (date.fromisoformat(str(r[close])[:10]) - date.fromisoformat(str(r[req])[:10])).days
        if d < 0:
            continue
        all_days.append(d)
        per_type.setdefault(TYPE_LABELS.get(r.get(typ), str(r.get(typ) or "Otro")), []).append(d)
    avg = sum(all_days) / len(all_days) if all_days else None
    return avg, {k: round(sum(v) / len(v), 1) for k, v in per_type.items()}


async def _groups(ctx: Ctx, period: Any, gb: str, extra: list[Any] | None = None) -> list[dict[str, Any]]:
    mr = M.MAINT_REQUEST
    dom = await build_domain(ctx, mr, "Peticiones de mantenimiento", period=period,
                             extra=list(M.MAINT_EXCLUDE_DOMAIN) + (extra or []))
    return await ctx.odoo.read_group(mr.model, dom, ["__count"], [gb])


async def _monthly_count(ctx: Ctx, period: Any) -> dict[str, float]:
    mr = M.MAINT_REQUEST
    gb = f"{mr.date_field}:month"
    dom = await build_domain(ctx, mr, "Peticiones por mes", period=period, extra=list(M.MAINT_EXCLUDE_DOMAIN))
    groups = await ctx.odoo.read_group(mr.model, dom, ["__count"], [gb])
    out: dict[str, float] = {}
    for g in groups:
        k = group_month_key(g, gb)
        if k:
            out[k] = out.get(k, 0) + int(g.get("__count", 0))
    return out


async def _cost(ctx: Ctx, period: Any) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
    """Costo de reparación: campo personalizado en la petición o, por defecto, repair.order."""
    mr = M.MAINT_REQUEST
    if mr.has("cost"):
        mm, amount, equip, label = mr, mr.f("cost"), mr.f("equipment"), "Costo de mantenimiento"
        extra = list(M.MAINT_EXCLUDE_DOMAIN)
    else:
        ro = M.REPAIR_ORDER
        reason = await ensure_model(ctx, ro)
        if reason:
            raise KeyError(reason)
        mm, amount, equip, label = ro, ro.f("amount"), ro.f("equipment"), "Órdenes de reparación"
        extra = []
    by_month = await monthly(ctx, mm, label, amount, period, extra=extra)
    dom = await build_domain(ctx, mm, label, period=period, extra=extra)
    groups = await ctx.odoo.read_group(mm.model, dom, [f"{amount}:sum"], [equip])
    top = top_groups(groups, equip, amount, n=10)
    return sum(by_month.values()), by_month, top


async def build(ctx: Ctx) -> Result:
    reason = await ensure_model(ctx, M.MAINT_REQUEST)
    if reason:
        raise ModuleUnavailable(reason)
    mr = M.MAINT_REQUEST
    cur, prev = periods(ctx.filters)

    async def none() -> None:
        return None

    (total_cur, total_prev, open_cur, closed_cur, corrective_cur, corrective_prev, by_stage, by_type,
     failures, res_cur, res_prev, cost_cur, cost_prev, req_month, req_month_prev) = await asyncio.gather(
        _count(ctx, cur, []),
        _count(ctx, prev, []) if prev else none(),
        _count(ctx, cur, list(M.MAINT_OPEN_DOMAIN)),
        _count(ctx, cur, list(M.MAINT_CLOSED_DOMAIN)),
        _count(ctx, cur, [(mr.f("type"), "=", "corrective")]),
        _count(ctx, prev, [(mr.f("type"), "=", "corrective")]) if prev else none(),
        _groups(ctx, cur, mr.f("stage")),
        _groups(ctx, cur, mr.f("type")),
        _groups(ctx, cur, mr.f("equipment"), [(mr.f("type"), "=", "corrective")]),
        safe(ctx, "Tiempo de resolución", _resolution(ctx, cur)),
        safe(ctx, "Tiempo de resolución", _resolution(ctx, prev)) if prev else none(),
        safe(ctx, "Costo de reparación", _cost(ctx, cur)),
        safe(ctx, "Costo de reparación", _cost(ctx, prev)) if prev else none(),
        _monthly_count(ctx, cur),
        _monthly_count(ctx, prev) if prev else none(),
    )

    cards = [
        card("peticiones", "Peticiones registradas", total_cur, total_prev, unit="count", higher_is_better=False),
        card("abiertas", "Abiertas (del periodo)", open_cur, unit="count", higher_is_better=False),
        card("cerradas", "Cerradas (del periodo)", closed_cur, unit="count"),
        card("correctivos_pct", "% correctivo", (corrective_cur / total_cur * 100) if total_cur else None,
             (corrective_prev / total_prev * 100) if (prev and total_prev and corrective_prev is not None) else None,
             unit="%", higher_is_better=False, hint="Correctivo / total de peticiones"),
    ]
    charts: list[Chart] = [monthly_chart("peticiones_mes", "Peticiones por mes", req_month, cur, req_month_prev, prev,
                                         unit="count")]
    tables: list[Table] = []

    if res_cur is not None:
        avg, per_type = res_cur
        cards.append(card("resolucion_dias", "Tiempo promedio de resolución", avg, res_prev[0] if res_prev else None,
                          unit="days", higher_is_better=False,
                          hint=" · ".join(f"{k}: {v} d" for k, v in per_type.items()) or None))

    stage_rows = top_groups([{**g, "valor": g.get("__count", 0)} for g in by_stage], mr.f("stage"), "valor", n=12)
    charts.append(Chart(key="peticiones_etapa", title="Peticiones por etapa", type="bar", unit="count",
                        series=[ChartSeries(key="valor", label="Peticiones")],
                        data=[{"label": r["label"], "valor": r["conteo"]} for r in stage_rows]))
    charts.append(Chart(key="preventivo_correctivo", title="Preventivo vs correctivo", type="donut", unit="count",
                        series=[ChartSeries(key="valor", label="Peticiones")],
                        data=[{"label": TYPE_LABELS.get(g.get(mr.f("type")), m2o_label(g.get(mr.f("type")))),
                               "valor": int(g.get("__count", 0))} for g in by_type]))
    fail_rows = top_groups([{**g, "valor": g.get("__count", 0)} for g in failures], mr.f("equipment"), "valor", n=10)
    charts.append(Chart(key="equipos_fallas", title="Top 10 equipos con más fallas (correctivos)", type="hbar", unit="count",
                        series=[ChartSeries(key="valor", label="Correctivos")],
                        data=[{"label": r["label"], "valor": r["conteo"]} for r in fail_rows]))

    if cost_cur is not None:
        total_cost, by_month, top = cost_cur
        cards.append(card("costo_reparacion", "Costo de reparación (sin IVA)", total_cost,
                          cost_prev[0] if cost_prev else None, higher_is_better=False))
        charts.append(monthly_chart("costo_mes", "Costo de reparación por mes", by_month, cur,
                                    cost_prev[1] if cost_prev else None, prev, chart_type="bar"))
        charts.append(bar_chart("costo_equipo", "Top 10 equipos por costo de reparación (de menor a mayor)",
                                sorted(top, key=lambda r: r["valor"])))
    return cards, charts, tables
