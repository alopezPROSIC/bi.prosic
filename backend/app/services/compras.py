"""KPIs de Compras / proveedores."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, KpiCard, Table
from .common import (
    Ctx,
    ModuleUnavailable,
    aging,
    bar_chart,
    build_domain,
    card,
    ensure_model,
    monthly,
    monthly_chart,
    num,
    periods,
    safe,
    top_groups,
    total,
)

Result = tuple[list[KpiCard], list[Chart], list[Table]]


async def _suppliers(ctx: Ctx, period: Any) -> list[dict[str, Any]]:
    pr = M.PURCHASE_REPORT
    amount, partner = pr.f("amount"), pr.f("partner")
    dom = await build_domain(ctx, pr, "Compras", period=period)
    groups = await ctx.odoo.read_group(pr.model, dom, [f"{amount}:sum"], [partner])
    return top_groups(groups, partner, amount, n=10_000)


async def _pending(ctx: Ctx) -> dict[str, float]:
    """Órdenes confirmadas (últimos N días) con cantidades por recibir o por facturar."""
    pr = M.PURCHASE_REPORT
    start = date.today() - timedelta(days=M.PURCHASE_PENDING_LOOKBACK_DAYS)
    dom = await build_domain(ctx, pr, "Órdenes pendientes", period=(start, date.today()))
    qo, qr, qb, amount, order = pr.f("qty_ordered"), pr.f("qty_received"), pr.f("qty_to_bill"), pr.f("amount"), pr.f("order")
    groups = await ctx.odoo.read_group(pr.model, dom, [f"{qo}:sum", f"{qr}:sum", f"{qb}:sum", f"{amount}:sum"], [order])
    out = {"recibir_n": 0.0, "recibir_monto": 0.0, "facturar_n": 0.0, "facturar_monto": 0.0}
    for g in groups:
        ordered, received, to_bill, amt = num(g.get(qo)), num(g.get(qr)), num(g.get(qb)), num(g.get(amount))
        if ordered <= 0:
            continue
        if received < ordered - 1e-6:
            out["recibir_n"] += 1
            out["recibir_monto"] += amt * (1 - received / ordered)
        if to_bill > 1e-6:
            out["facturar_n"] += 1
            out["facturar_monto"] += amt * min(1.0, to_bill / ordered)
    return out


async def build(ctx: Ctx) -> Result:
    reason = await ensure_model(ctx, M.PURCHASE_REPORT)
    if reason:
        raise ModuleUnavailable(reason)
    pr, vb = M.PURCHASE_REPORT, M.VENDOR_BILL
    cur, prev = periods(ctx.filters)
    amount = pr.f("amount")

    async def none() -> None:
        return None

    async def bills(period: Any) -> float:
        v, _ = await total(ctx, vb, "Facturas de proveedor", vb.f("total"), period,
                           extra=[(vb.f("move_type"), "in", ["in_invoice", "in_refund"])])
        return abs(v)

    (spend_cur, _), spend_prev, m_cur, m_prev, suppliers, pending, bills_cur, bills_prev, ap = await asyncio.gather(
        total(ctx, pr, "Compras", amount, cur),
        total(ctx, pr, "Compras", amount, prev) if prev else none(),
        monthly(ctx, pr, "Compras", amount, cur),
        monthly(ctx, pr, "Compras", amount, prev) if prev else none(),
        _suppliers(ctx, cur),
        safe(ctx, "Órdenes pendientes", _pending(ctx)),
        safe(ctx, "Facturas de proveedor", bills(cur)),
        safe(ctx, "Facturas de proveedor", bills(prev)) if prev else none(),
        safe(ctx, "Cuentas por pagar", aging(ctx, vb, "Cuentas por pagar", ["in_invoice", "in_refund"],
                                             "proveedor", "Cuentas por pagar", "cxp")),
    )
    spend_prev_v = spend_prev[0] if spend_prev else None
    top10 = suppliers[:10]
    total_sup = sum(r["valor"] for r in suppliers)
    top1_share = (suppliers[0]["valor"] / total_sup * 100) if suppliers and total_sup else None
    top5_share = (sum(r["valor"] for r in suppliers[:5]) / total_sup * 100) if total_sup else None

    cards = [
        card("gasto", "Compras confirmadas (sin IVA)", spend_cur, spend_prev_v, higher_is_better=False),
        card("proveedores_activos", "Proveedores con compras", len(suppliers), unit="count"),
        card("concentracion_top1", "Concentración proveedor #1", top1_share, unit="%", higher_is_better=False),
        card("concentracion_top5", "Concentración top 5", top5_share, unit="%", higher_is_better=False),
    ]
    if bills_cur is not None:
        cards.append(card("facturado_proveedores", "Facturado por proveedores (con IVA)", bills_cur, bills_prev,
                          higher_is_better=False))
    if pending is not None:
        cards += [
            card("pendiente_recibir", "Por recibir (monto est.)", pending["recibir_monto"], higher_is_better=False,
                 hint=f"{int(pending['recibir_n'])} órdenes, últimos {M.PURCHASE_PENDING_LOOKBACK_DAYS} días"),
            card("pendiente_facturar", "Por facturar (monto est.)", pending["facturar_monto"], higher_is_better=False,
                 hint=f"{int(pending['facturar_n'])} órdenes, últimos {M.PURCHASE_PENDING_LOOKBACK_DAYS} días"),
        ]
    charts = [
        monthly_chart("compras_mes", "Compras por mes", m_cur, cur, m_prev, prev),
        bar_chart("top_proveedores", "Top 10 proveedores por gasto", top10, entity="proveedor"),
    ]
    tables: list[Table] = []
    if ap is not None:
        totals, table, chart, as_of = ap
        total_ap = sum(totals.values())
        overdue = total_ap - totals["por_vencer"]
        cards += [
            card("cxp_total", "Cuentas por pagar", total_ap, higher_is_better=False, hint=f"Corte {as_of.isoformat()}"),
            card("cxp_vencida", "CxP vencida", overdue, higher_is_better=False),
        ]
        charts.append(chart)
        tables.append(table)
    return cards, charts, tables
