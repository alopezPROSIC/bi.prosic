"""KPIs de Ventas y facturación."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, ChartSeries, KpiCard, Table
from .common import (
    Ctx,
    ModuleUnavailable,
    aging,
    bar_chart,
    build_domain,
    card,
    ensure_model,
    month_label,
    monthly,
    monthly_chart,
    months_between,
    periods,
    safe,
    top_groups,
    total,
)

Result = tuple[list[KpiCard], list[Chart], list[Table]]
DSO_DAYS = 90


async def _top(ctx: Ctx, key: str, period: Any, n: int = 10) -> list[dict[str, Any]]:
    sr = M.SALE_REPORT
    amount, gb = sr.f("amount"), sr.f(key)
    dom = await build_domain(ctx, sr, "Ventas", period=period)
    groups = await ctx.odoo.read_group(sr.model, dom, [f"{amount}:sum"], [gb])
    return top_groups(groups, gb, amount, n=n)


async def build(ctx: Ctx) -> Result:
    reason = await ensure_model(ctx, M.SALE_REPORT)
    if reason:
        raise ModuleUnavailable(reason)
    sr, so, inv, pay = M.SALE_REPORT, M.SALE_ORDER, M.CUSTOMER_INVOICE, M.CUSTOMER_PAYMENT
    cur, prev = periods(ctx.filters)
    amount = sr.f("amount")

    async def order_count(period: Any) -> int:
        dom = await build_domain(ctx, so, "Pedidos de venta", period=period)
        return await ctx.odoo.search_count(so.model, dom)

    async def invoiced(period: Any) -> float:
        v, _ = await total(ctx, inv, "Facturación", inv.f("untaxed"), period,
                           extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"])])
        return v

    async def credit_notes(period: Any) -> tuple[float, int]:
        v, n = await total(ctx, inv, "Notas de crédito", inv.f("untaxed"), period,
                           extra=[(inv.f("move_type"), "=", "out_refund")])
        return abs(v), n

    async def collected(period: Any) -> float:
        v, _ = await total(ctx, pay, "Cobranza", pay.f("amount"), period)
        return v

    async def maybe(coro_fn, period):  # noqa: ANN001
        return await coro_fn(period) if period else None

    async def invoiced_with_tax(period: Any) -> float:
        """Facturado CON IVA (comparable con el saldo de CxC) para el DSO."""
        v, _ = await total(ctx, inv, "Facturación", inv.f("total"), period,
                           extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"])])
        return v

    as_of = min(ctx.filters.date_to, date.today())
    dso_window = (as_of - timedelta(days=DSO_DAYS - 1), as_of)

    (
        (sales_cur, _), sales_prev, orders_cur, orders_prev, monthly_cur, monthly_prev,
        top_clients, by_seller, by_categ, by_product,
        inv_cur, inv_prev, cn_cur, cn_prev, col_cur, col_prev, inv_month, col_month, ar, inv_dso,
    ) = await asyncio.gather(
        total(ctx, sr, "Ventas", amount, cur),
        maybe(lambda p: total(ctx, sr, "Ventas", amount, p), prev),
        safe(ctx, "Pedidos de venta", order_count(cur)),
        safe(ctx, "Pedidos de venta", maybe(order_count, prev)),
        monthly(ctx, sr, "Ventas", amount, cur),
        maybe(lambda p: monthly(ctx, sr, "Ventas", amount, p), prev),
        _top(ctx, "partner", cur),
        _top(ctx, "salesperson", cur),
        safe(ctx, "Ventas por categoría", _top(ctx, "category", cur)),
        safe(ctx, "Ventas por producto", _top(ctx, "product", cur)),
        safe(ctx, "Facturación", invoiced(cur)),
        safe(ctx, "Facturación", maybe(invoiced, prev)),
        safe(ctx, "Notas de crédito", credit_notes(cur)),
        safe(ctx, "Notas de crédito", maybe(credit_notes, prev)),
        safe(ctx, "Cobranza", collected(cur)),
        safe(ctx, "Cobranza", maybe(collected, prev)),
        safe(ctx, "Facturado vs cobrado", monthly(ctx, inv, "Facturación", inv.f("untaxed"), cur,
                                                 extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"])])),
        safe(ctx, "Facturado vs cobrado", monthly(ctx, pay, "Cobranza", pay.f("amount"), cur)),
        safe(ctx, "Cuentas por cobrar", aging(ctx, inv, "Cuentas por cobrar", ["out_invoice", "out_refund"],
                                              "cliente", "Cuentas por cobrar", "cxc")),
        safe(ctx, "DSO", invoiced_with_tax(dso_window)),
    )
    sales_prev_v = sales_prev[0] if sales_prev else None

    ticket = (sales_cur / orders_cur) if orders_cur else None
    ticket_prev = (sales_prev_v / orders_prev) if (sales_prev_v is not None and orders_prev) else None

    cards = [
        card("ventas", "Ventas (sin IVA)", sales_cur, sales_prev_v),
        card("pedidos", "Pedidos confirmados", orders_cur, orders_prev, unit="count"),
        card("ticket_promedio", "Ticket promedio", ticket, ticket_prev),
    ]
    if inv_cur is not None:
        cards.append(card("facturado", "Facturado neto (sin IVA)", inv_cur, inv_prev,
                          hint="Facturas menos notas de crédito publicadas"))
    if col_cur is not None:
        cards.append(card("cobrado", "Cobrado", col_cur, col_prev, hint="Pagos de clientes publicados (con IVA)"))
    if cn_cur is not None:
        cards.append(card("notas_credito", "Notas de crédito", cn_cur[0], cn_prev[0] if cn_prev else None,
                          higher_is_better=False, hint=f"{cn_cur[1]} documentos"))

    charts: list[Chart] = [monthly_chart("ventas_mes", "Ventas por mes", monthly_cur, cur, monthly_prev, prev)]
    tables: list[Table] = []
    if inv_month is not None and col_month is not None:
        charts.append(Chart(
            key="facturado_vs_cobrado", title="Facturado vs cobrado por mes", type="bar",
            series=[ChartSeries(key="facturado", label="Facturado (sin IVA)"), ChartSeries(key="cobrado", label="Cobrado")],
            data=[{"label": month_label(m), "facturado": round(inv_month.get(m, 0), 2), "cobrado": round(col_month.get(m, 0), 2)}
                  for m in months_between(*cur)],
        ))
    charts.append(bar_chart("top_clientes", "Top 10 clientes", top_clients, entity="cliente"))
    charts.append(bar_chart("ventas_vendedor", "Ventas por vendedor", by_seller, entity="vendedor"))
    if by_categ is not None:
        charts.append(bar_chart("ventas_categoria", "Ventas por categoría de producto (top 10)", by_categ))
    if by_product is not None:
        charts.append(bar_chart("ventas_producto", "Top 10 productos", by_product))

    if ar is not None:
        totals, table, chart, as_of = ar
        total_ar = sum(totals.values())
        overdue = total_ar - totals["por_vencer"]
        cards += [
            card("cxc_total", "Cuentas por cobrar", total_ar, hint=f"Corte {as_of.isoformat()}"),
            card("cxc_vencida", "CxC vencida", overdue, higher_is_better=False),
            card("cxc_vencida_pct", "% de CxC vencida", (overdue / total_ar * 100) if total_ar else None,
                 unit="%", higher_is_better=False),
        ]
        if inv_dso:
            cards.append(card("cxc_dso", "DSO (días promedio de cobro)", total_ar / inv_dso * DSO_DAYS, unit="days",
                              higher_is_better=False,
                              hint=f"CxC / facturado con IVA de los últimos {DSO_DAYS} días × {DSO_DAYS}"))
        charts.append(chart)
        tables.append(table)
    return cards, charts, tables
