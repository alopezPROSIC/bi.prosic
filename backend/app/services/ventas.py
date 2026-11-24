"""KPIs de Ventas y facturación (y la ficha de un vendedor, que reutiliza los mismos cálculos).

Con o sin IVA (filtro ``iva``):
- Ventas (pedidos) y facturado se muestran según el filtro.
- Cobrado y cartera son dinero real: siempre con IVA.
- Facturado neto = líneas de facturas − notas de crédito, después del descuento de cada línea y SIN anticipos
  (las facturas de anticipo van a una cuenta de pasivo, no son venta).
"""
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
    group_month_key,
    m2o_id,
    m2o_label,
    month_label,
    monthly,
    monthly_chart,
    months_between,
    num,
    periods,
    safe,
    top_groups,
    total,
)

Result = tuple[list[KpiCard], list[Chart], list[Table]]
DSO_DAYS = 90
NO_ADVANCES = [("account_id.code", "not in", M.SALES_ADVANCE_ACCOUNTS)]
ONLY_ADVANCES = [("account_id.code", "in", M.SALES_ADVANCE_ACCOUNTS)]


def with_tax(ctx: Ctx) -> bool:
    return ctx.filters.iva == "con"


def tax_label(ctx: Ctx) -> str:
    return "con IVA" if with_tax(ctx) else "sin IVA"


def concept(account: str) -> str:
    """Concepto de ingreso según la cuenta contable («401-001-001 Renta - …» → «Renta»)."""
    code = account.split(" ", 1)[0]
    if code.startswith("402"):
        return "Notas de crédito y descuentos"
    if code.startswith("401-001"):
        return "Renta"
    if code.startswith("401-002"):
        return "Maniobras"
    if code.startswith("401-003"):
        return "Refacciones y servicios"
    if code == "401-005-005":
        return "Seguros"
    if code.startswith("401-004") or code.startswith("700-704"):
        return "Venta de equipo"
    return "Otros ingresos"


# ------------------------------------------------------------------ filtros por vendedor
def seller_sale(ctx: Ctx) -> list[Any]:
    return [("user_id", "=", ctx.seller_id)] if ctx.seller_id else []


def seller_move(ctx: Ctx) -> list[Any]:
    return [("invoice_user_id", "=", ctx.seller_id)] if ctx.seller_id else []


def seller_line(ctx: Ctx) -> list[Any]:
    return [("move_id.invoice_user_id", "=", ctx.seller_id)] if ctx.seller_id else []


# ------------------------------------------------------------------ líneas de factura (sin / con IVA)
async def invoice_lines(ctx: Ctx, period: Any, groupby: list[str], extra: list[Any] | None = None
                        ) -> list[tuple[dict[str, Any], float]]:
    """[(grupo, importe neto)] de las líneas de facturas de cliente (notas de crédito en negativo).
    Sin IVA: −balance (moneda de la compañía, después del descuento). Con IVA: price_total de facturas −
    price_total de notas de crédito."""
    il = M.INVOICE_LINE
    dom = await build_domain(ctx, il, "Facturación", period=period, extra=[*(extra or []), *seller_line(ctx)])
    if not with_tax(ctx):
        groups = await ctx.odoo.read_group(il.model, dom, ["balance:sum"], groupby)
        return [(g, -num(g.get("balance"))) for g in groups]
    out: list[tuple[dict[str, Any], float]] = []
    for move_type, sign in (("out_invoice", 1), ("out_refund", -1)):
        groups = await ctx.odoo.read_group(il.model, dom + [("move_id.move_type", "=", move_type)],
                                           ["price_total:sum"], groupby)
        out += [(g, sign * num(g.get("price_total"))) for g in groups]
    return out


async def invoiced(ctx: Ctx, period: Any) -> float:
    return sum(v for _, v in await invoice_lines(ctx, period, [], NO_ADVANCES))


async def invoiced_by_month(ctx: Ctx, period: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for g, v in await invoice_lines(ctx, period, ["date:month"], NO_ADVANCES):
        k = group_month_key(g, "date:month")
        if k:
            out[k] = out.get(k, 0.0) + v
    return out


async def by_concept(ctx: Ctx, period: Any) -> tuple[list[dict[str, Any]], float]:
    """(facturado neto por concepto, anticipos facturados) del periodo."""
    out: dict[str, float] = {}
    advances = 0.0
    for g, value in await invoice_lines(ctx, period, ["account_id"]):
        name = m2o_label(g.get("account_id"))
        if name.split(" ", 1)[0] in M.SALES_ADVANCE_ACCOUNTS:
            advances += value
        else:
            out[concept(name)] = out.get(concept(name), 0.0) + value
    return [{"label": k, "valor": round(v, 2)} for k, v in out.items() if round(v, 2)], advances


async def invoiced_by_partner(ctx: Ctx, period: Any, n: int = 10) -> list[dict[str, Any]]:
    rows: dict[Any, dict[str, Any]] = {}
    for g, v in await invoice_lines(ctx, period, ["partner_id"], NO_ADVANCES):
        pid = m2o_id(g.get("partner_id"))
        r = rows.setdefault(pid, {"label": m2o_label(g.get("partner_id")), "id": pid, "valor": 0.0})
        r["valor"] += v
    out = sorted(rows.values(), key=lambda r: -r["valor"])[:n]
    return [{**r, "valor": round(r["valor"], 2)} for r in out]


# ------------------------------------------------------------------ pedidos (sale.report)
def sale_amount(ctx: Ctx) -> str:
    return "price_total" if with_tax(ctx) else M.SALE_REPORT.f("amount")


async def top_sales(ctx: Ctx, key: str, period: Any, n: int = 10) -> list[dict[str, Any]]:
    sr = M.SALE_REPORT
    amount, gb = sale_amount(ctx), sr.f(key)
    dom = await build_domain(ctx, sr, "Ventas", period=period, extra=seller_sale(ctx))
    groups = await ctx.odoo.read_group(sr.model, dom, [f"{amount}:sum"], [gb])
    return top_groups(groups, gb, amount, n=n)


async def build(ctx: Ctx) -> Result:
    reason = await ensure_model(ctx, M.SALE_REPORT)
    if reason:
        raise ModuleUnavailable(reason)
    sr, so, inv, pay = M.SALE_REPORT, M.SALE_ORDER, M.CUSTOMER_INVOICE, M.CUSTOMER_PAYMENT
    cur, prev = periods(ctx.filters)
    amount, iva = sale_amount(ctx), tax_label(ctx)
    seller = bool(ctx.seller_id)

    async def order_count(period: Any) -> int:
        dom = await build_domain(ctx, so, "Pedidos de venta", period=period, extra=seller_sale(ctx))
        return await ctx.odoo.search_count(so.model, dom)

    async def credit_notes(period: Any) -> tuple[float, int]:
        field = inv.f("total") if with_tax(ctx) else inv.f("untaxed")
        v, n = await total(ctx, inv, "Notas de crédito", field, period,
                           extra=[(inv.f("move_type"), "=", "out_refund"), *seller_move(ctx)])
        return abs(v), n

    async def collected(period: Any) -> float:
        v, _ = await total(ctx, pay, "Cobranza", pay.f("amount"), period)
        return v

    async def maybe(coro_fn, period):  # noqa: ANN001
        return await coro_fn(period) if period else None

    async def invoiced_with_tax(period: Any) -> float:
        """Facturado CON IVA (comparable con el saldo de CxC) para el DSO."""
        v, _ = await total(ctx, inv, "Facturación", inv.f("total"), period,
                           extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"]), *seller_move(ctx)])
        return v

    async def sales_total(period: Any) -> tuple[float, int]:
        return await total(ctx, sr, "Ventas", amount, period, extra=seller_sale(ctx))

    async def sales_month(period: Any) -> dict[str, float]:
        return await monthly(ctx, sr, "Ventas", amount, period, extra=seller_sale(ctx))

    async def nothing() -> None:
        return None

    as_of = min(ctx.filters.date_to, date.today())
    dso_window = (as_of - timedelta(days=DSO_DAYS - 1), as_of)

    (
        (sales_cur, _), sales_prev, orders_cur, orders_prev, monthly_cur, monthly_prev,
        top_clients, by_seller, by_categ, by_product,
        inv_cur, inv_prev, cn_cur, cn_prev, col_cur, col_prev, inv_month, col_month, ar, inv_dso, concepts,
    ) = await asyncio.gather(
        sales_total(cur),
        maybe(sales_total, prev),
        safe(ctx, "Pedidos de venta", order_count(cur)),
        safe(ctx, "Pedidos de venta", maybe(order_count, prev)),
        sales_month(cur),
        maybe(sales_month, prev),
        # Ficha del vendedor: sus clientes por FACTURADO (lo que realmente generó); módulo: por pedidos.
        safe(ctx, "Top clientes", invoiced_by_partner(ctx, cur)) if seller else top_sales(ctx, "partner", cur),
        nothing() if seller else top_sales(ctx, "salesperson", cur),
        safe(ctx, "Ventas por categoría", top_sales(ctx, "category", cur)),
        safe(ctx, "Ventas por producto", top_sales(ctx, "product", cur)),
        safe(ctx, "Facturación", invoiced(ctx, cur)),
        safe(ctx, "Facturación", maybe(lambda p: invoiced(ctx, p), prev)),
        safe(ctx, "Notas de crédito", credit_notes(cur)),
        safe(ctx, "Notas de crédito", maybe(credit_notes, prev)),
        nothing() if seller else safe(ctx, "Cobranza", collected(cur)),
        nothing() if seller else safe(ctx, "Cobranza", maybe(collected, prev)),
        safe(ctx, "Facturado por mes", invoiced_by_month(ctx, cur)),
        nothing() if seller else safe(ctx, "Facturado vs cobrado", monthly(ctx, pay, "Cobranza", pay.f("amount"), cur)),
        safe(ctx, "Cuentas por cobrar", aging(ctx, inv, "Cuentas por cobrar", ["out_invoice", "out_refund"],
                                              "cliente", "Cuentas por cobrar", "cxc", extra=seller_move(ctx))),
        safe(ctx, "DSO", invoiced_with_tax(dso_window)),
        safe(ctx, "Facturado por concepto", by_concept(ctx, cur)),
    )
    sales_prev_v = sales_prev[0] if sales_prev else None
    ticket = (sales_cur / orders_cur) if orders_cur else None
    ticket_prev = (sales_prev_v / orders_prev) if (sales_prev_v is not None and orders_prev) else None

    cards: list[KpiCard] = []
    if inv_cur is not None:
        cards.append(card("facturado", f"Facturado neto ({iva})", inv_cur, inv_prev,
                          hint="Facturas menos notas de crédito publicadas, después de descuentos y sin anticipos"))
    cards += [
        card("ventas", f"Ventas en pedidos ({iva})", sales_cur, sales_prev_v,
             hint="Pedidos de venta confirmados en el periodo (una renta se confirma una vez y se factura cada mes)"),
        card("pedidos", "Pedidos confirmados", orders_cur, orders_prev, unit="count"),
        card("ticket_promedio", f"Ticket promedio ({iva})", ticket, ticket_prev),
    ]
    if concepts is not None and concepts[1] > 0.5:  # negativo = anticipos cancelados con nota de crédito
        cards.append(card("anticipos", f"Anticipos facturados ({iva}, no son venta)", concepts[1],
                          hint="Facturas de anticipo (cuenta 200-206-001): no suman al facturado neto"))
    if col_cur is not None:
        cards.append(card("cobrado", "Cobrado (con IVA)", col_cur, col_prev,
                          hint="Pagos de clientes publicados: dinero real, siempre con IVA"))
    if cn_cur is not None:
        cards.append(card("notas_credito", f"Notas de crédito ({iva})", cn_cur[0], cn_prev[0] if cn_prev else None,
                          higher_is_better=False, hint=f"{cn_cur[1]} documentos"))

    charts: list[Chart] = []
    tables: list[Table] = []
    if inv_month is not None and col_month is not None:
        charts.append(Chart(
            key="facturado_vs_cobrado", title=f"Facturado ({iva}) vs cobrado (con IVA) por mes", type="bar", ordered=True,
            series=[ChartSeries(key="facturado", label=f"Facturado ({iva})"), ChartSeries(key="cobrado", label="Cobrado")],
            data=[{"label": month_label(m), "facturado": round(inv_month.get(m, 0), 2), "cobrado": round(col_month.get(m, 0), 2)}
                  for m in months_between(*cur)],
        ))
    elif inv_month is not None:
        charts.append(monthly_chart("facturado_mes", f"Facturado neto por mes ({iva})", inv_month, cur, chart_type="bar"))
    if concepts is not None and concepts[0]:
        charts.append(bar_chart("facturado_concepto", f"Facturado neto por concepto ({iva})", concepts[0]))
    charts.append(monthly_chart("ventas_mes", f"Ventas en pedidos por mes ({iva})", monthly_cur, cur, monthly_prev, prev))
    if top_clients is not None:
        title = f"Top 10 clientes por facturado ({iva})" if seller else f"Top 10 clientes por pedidos ({iva})"
        charts.append(bar_chart("top_clientes", title, top_clients, entity="cliente"))
    if by_seller is not None:
        ch = bar_chart("ventas_vendedor", f"Ventas en pedidos por vendedor ({iva}) — clic para ver su ficha",
                       by_seller, entity="vendedor")
        ch.drill = "vendedor"
        ch.data = [{**d, "_id": r["id"]} for d, r in zip(ch.data, by_seller)]
        charts.append(ch)
    if by_categ is not None:
        charts.append(bar_chart("ventas_categoria", f"Ventas por categoría de producto (top 10, {iva})", by_categ))
    if by_product is not None:
        charts.append(bar_chart("ventas_producto", f"Top 10 productos ({iva})", by_product))

    if ar is not None:
        totals, table, chart, as_of = ar
        total_ar = sum(totals.values())
        overdue = total_ar - totals["por_vencer"]
        cards += [
            card("cxc_total", "Cuentas por cobrar (con IVA)", total_ar, hint=f"Corte {as_of.isoformat()}"),
            card("cxc_vencida", "CxC vencida (con IVA)", overdue, higher_is_better=False),
            card("cxc_vencida_pct", "% de CxC vencida", (overdue / total_ar * 100) if total_ar else None,
                 unit="%", higher_is_better=False),
        ]
        if inv_dso:
            cards.append(card("cxc_dso", "DSO (días promedio de cobro)", total_ar / inv_dso * DSO_DAYS, unit="days",
                              higher_is_better=False,
                              hint=f"CxC / facturado con IVA de los últimos {DSO_DAYS} días × {DSO_DAYS}"))
        charts.append(chart)
        tables.append(table)
    if with_tax(ctx):
        ctx.note("Importes con IVA: pedidos y facturado usan el total de cada línea con impuestos; cobrado y cartera "
                 "siempre son con IVA (dinero real).")
    else:
        ctx.note("Importes sin IVA (después de descuentos). Cobrado y cartera se muestran con IVA porque son dinero real.")
    return cards, charts, tables


# ------------------------------------------------------------------ ranking de vendedores
async def seller_ranking(ctx: Ctx, period: Any) -> list[dict[str, Any]]:
    """Facturado neto (sin anticipos) y pedidos por vendedor del periodo, de mayor a menor."""
    inv = M.CUSTOMER_INVOICE
    field = inv.f("total") if with_tax(ctx) else inv.f("untaxed")
    base = [(inv.f("move_type"), "in", ["out_invoice", "out_refund"])]
    dom = await build_domain(ctx, inv, "Facturación", period=period, extra=base)
    adv_dom = dom + [("line_ids.account_id.code", "in", M.SALES_ADVANCE_ACCOUNTS)]
    sr = M.SALE_REPORT
    sdom = await build_domain(ctx, sr, "Ventas", period=period)
    groups, advances, sales = await asyncio.gather(
        ctx.odoo.read_group(inv.model, dom, [f"{field}:sum"], ["invoice_user_id"]),
        ctx.odoo.read_group(inv.model, adv_dom, [f"{field}:sum"], ["invoice_user_id"]),
        ctx.odoo.read_group(sr.model, sdom, [f"{sale_amount(ctx)}:sum", "order_id:count_distinct"], [sr.f("salesperson")]),
    )
    rows: dict[Any, dict[str, Any]] = {}

    def row(v: Any) -> dict[str, Any]:
        sid = m2o_id(v)
        return rows.setdefault(sid, {"id": sid, "nombre": m2o_label(v), "facturado": 0.0, "pedidos_monto": 0.0,
                                     "facturas": 0, "pedidos": 0})
    for g in groups:
        r = row(g.get("invoice_user_id"))
        r["facturado"] += num(g.get(field))
        r["facturas"] += int(g.get("invoice_user_id_count") or g.get("__count") or 0)
    for g in advances:
        row(g.get("invoice_user_id"))["facturado"] -= num(g.get(field))
    for g in sales:
        r = row(g.get(sr.f("salesperson")))
        r["pedidos_monto"] += num(g.get(sale_amount(ctx)))
        r["pedidos"] += int(g.get("order_id") or 0)
    out = sorted(rows.values(), key=lambda r: -r["facturado"])
    tot = sum(r["facturado"] for r in out) or 0
    for i, r in enumerate(out, 1):
        r["rank"] = i
        r["participacion"] = round(r["facturado"] / tot * 100, 2) if tot else None
        r["facturado"], r["pedidos_monto"] = round(r["facturado"], 2), round(r["pedidos_monto"], 2)
    return out
