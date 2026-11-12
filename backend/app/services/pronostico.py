"""Pronóstico a 6 meses (ventas, ingresos de renta, facturación, cobranza, compras y costo de reparación)
más el calendario de vencimientos (CxC, CxP y contratos de renta).

Método (determinista y explicable; la IA solo lo narra, no inventa cifras):
    pronóstico(mes t) = real(t − 12 meses) × crecimiento interanual reciente
    crecimiento = promedio últimos 3 meses cerrados / promedio de esos mismos 3 meses del año anterior
                  (acotado a 0.5–2.0 para evitar saltos por datos atípicos)
Si hay menos de 15 meses de historia se usa el promedio de los últimos 3 meses.
El rango (mín–máx) es ± el error porcentual medio (MAPE) de un backtest sobre los últimos 6 meses cerrados.
La historia termina en el último mes cerrado antes de la fecha «hasta» de los filtros.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, Table
from .common import Ctx, build_domain, card, group_month_key, month_label, monthly, num, safe

Result = tuple[list[KpiCard], list[Chart], list[Table]]
HISTORY_MONTHS = 24
HORIZON = 6
CHART_HISTORY = 12  # meses de historia que se grafican (más el horizonte)
BACKTEST = 6
GROWTH_BOUNDS = (0.5, 2.0)


def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def month_range(start: str, count: int) -> list[str]:
    return [add_months(start, i) for i in range(count)]


def _predict(values: list[float], h: int) -> float:
    """Predice el valor h meses (0 = el siguiente) después del final de ``values``."""
    n = len(values)
    if n == 0:
        return 0.0
    level = sum(values[-3:]) / min(3, n)
    t = n + h
    if n >= 15 and t - 12 < n:
        last_year = sum(values[n - 15:n - 12]) / 3
        growth = level / last_year if last_year > 0 else 1.0
        growth = min(max(growth, GROWTH_BOUNDS[0]), GROWTH_BOUNDS[1])
        return max(0.0, values[t - 12] * growth)
    return max(0.0, level)


def forecast(values: list[float], horizon: int = HORIZON) -> tuple[list[float], float | None, str]:
    """(pronósticos, error MAPE en % o None, método)."""
    preds = [_predict(values, h) for h in range(horizon)]
    errors = []
    # backtest: predecir cada uno de los últimos 6 meses con la historia previa (mínimo 3 meses de entrenamiento)
    for k in range(min(BACKTEST, len(values) - 3), 0, -1):
        train, actual = values[:-k], values[-k]
        if len(train) >= 3 and actual > 0:
            errors.append(abs(_predict(train, 0) - actual) / actual)
    mape = round(sum(errors) / len(errors) * 100, 1) if errors else None
    method = "estacional (mismo mes del año anterior × crecimiento)" if len(values) >= 15 else "promedio de 3 meses"
    return preds, mape, method


SERIES = [
    # clave, etiqueta, unidad, ¿mayor es mejor?
    ("ventas", "Ventas (sin IVA)", True),
    ("renta", "Ingresos de renta (sin IVA)", True),
    ("facturado", "Facturado neto (sin IVA)", True),
    ("cobranza", "Cobranza (con IVA)", True),
    ("compras", "Compras (sin IVA)", False),
    ("reparacion", "Costo de reparación (sin IVA)", False),
]


async def _history(ctx: Ctx, period: tuple[date, date]) -> dict[str, dict[str, float] | None]:
    sr, inv, pay, pr, ro = M.SALE_REPORT, M.CUSTOMER_INVOICE, M.CUSTOMER_PAYMENT, M.PURCHASE_REPORT, M.REPAIR_ORDER
    out = await asyncio.gather(
        safe(ctx, "Pronóstico: ventas", monthly(ctx, sr, "Ventas", sr.f("amount"), period)),
        safe(ctx, "Pronóstico: renta", monthly(ctx, sr, "Ingresos de renta", sr.f("amount"), period,
                                               extra=list(M.RENTAL_ORDER_FILTER))),
        safe(ctx, "Pronóstico: facturación", monthly(ctx, inv, "Facturación", inv.f("untaxed"), period,
                                                     extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"])])),
        safe(ctx, "Pronóstico: cobranza", monthly(ctx, pay, "Cobranza", pay.f("amount"), period)),
        safe(ctx, "Pronóstico: compras", monthly(ctx, pr, "Compras", pr.f("amount"), period)),
        safe(ctx, "Pronóstico: reparaciones", monthly(ctx, ro, "Órdenes de reparación", ro.f("amount"), period)),
    )
    return dict(zip([s[0] for s in SERIES], out))


async def _scheduled(ctx: Ctx, mm: Any, label: str, move_types: list[str], months: list[str],
                     as_of: date) -> tuple[dict[str, float], float]:
    """Saldo pendiente por mes de vencimiento (horizonte) y saldo ya vencido al corte."""
    residual, due = mm.f("residual"), mm.f("due_date")
    base = await build_domain(ctx, mm, label, extra=[(mm.f("move_type"), "in", move_types), (residual, "!=", 0)])
    end = add_months(months[-1], 1) + "-01"
    gb = f"{due}:month"
    groups, overdue = await asyncio.gather(
        ctx.odoo.read_group(mm.model, base + [(due, ">=", as_of.isoformat()), (due, "<", end)], [f"{residual}:sum"], [gb]),
        ctx.odoo.read_group(mm.model, base + [(due, "<", as_of.isoformat())], [f"{residual}:sum"], []),
    )
    by_month: dict[str, float] = {}
    for g in groups:
        k = group_month_key(g, gb)
        if k:
            by_month[k] = by_month.get(k, 0.0) + abs(num(g.get(residual)))
    return by_month, abs(num(overdue[0].get(residual))) if overdue else 0.0


async def _contract_expirations(ctx: Ctx, months: list[str], as_of: date) -> dict[str, tuple[int, float]]:
    rc = M.RENTAL_CONTRACT
    end_f = rc.f("end")
    gb = f"{end_f}:month"
    dom = await build_domain(ctx, rc, "Contratos por vencer", extra=[
        (rc.f("state"), "in", M.RENTAL_CONTRACT_ACTIVE_STATES), (end_f, ">=", as_of.isoformat()),
        (end_f, "<", add_months(months[-1], 1) + "-01")])
    fields = ["__count"] + ([f"{rc.f('price')}:sum"] if rc.has("price") else [])
    groups = await ctx.odoo.read_group(rc.model, dom, fields, [gb])
    out: dict[str, tuple[int, float]] = {}
    for g in groups:
        k = group_month_key(g, gb)
        if k:
            n, amt = out.get(k, (0, 0.0))
            out[k] = (n + int(g.get("__count", 0)), amt + (num(g.get(rc.f("price"))) if rc.has("price") else 0.0))
    return out


async def build(ctx: Ctx) -> Result:
    as_of = min(ctx.filters.date_to, date.today())
    current = f"{as_of.year}-{as_of.month:02d}"
    last_closed = add_months(current, -1)
    hist_months = month_range(add_months(last_closed, -(HISTORY_MONTHS - 1)), HISTORY_MONTHS)
    horizon = month_range(current, HORIZON)
    hist_period = (date.fromisoformat(hist_months[0] + "-01"), as_of)  # incluye el mes en curso (parcial)

    hist, ar, ap, contracts = await asyncio.gather(
        _history(ctx, hist_period),
        safe(ctx, "Calendario de cobros", _scheduled(ctx, M.CUSTOMER_INVOICE, "Cuentas por cobrar",
                                                     ["out_invoice", "out_refund"], horizon, as_of)),
        safe(ctx, "Calendario de pagos", _scheduled(ctx, M.VENDOR_BILL, "Cuentas por pagar",
                                                    ["in_invoice", "in_refund"], horizon, as_of)),
        safe(ctx, "Contratos por vencer", _contract_expirations(ctx, horizon, as_of)),
    )
    ctx.note(f"Pronóstico con {HISTORY_MONTHS} meses cerrados hasta {month_label(last_closed)}; "
             f"«{month_label(current)}» muestra lo real a la fecha ({as_of.isoformat()}) contra el pronóstico del mes completo.")

    cards: list[KpiCard] = []
    charts: list[Chart] = []
    preds: dict[str, list[float]] = {}
    mapes: dict[str, float | None] = {}
    for key, label, better in SERIES:
        series = hist.get(key)
        if series is None:
            continue
        values = [series.get(m, 0.0) for m in hist_months]
        if not any(values):
            ctx.note(f"Sin historia de «{label}»: no se pronostica.")
            continue
        p, mape, method = forecast(values)
        preds[key], mapes[key] = p, mape
        next3 = sum(p[:3])
        last3_ly = sum(series.get(add_months(m, -12), 0.0) for m in horizon[:3])
        cards.append(card(f"pron_{key}_3m", f"{label}: próximos 3 meses", next3, last3_ly or None, higher_is_better=better,
                          hint=f"vs mismos meses del año anterior · error histórico ±{mape:.0f}% · {method}"
                          if mape is not None else method))
        if key in ("ventas", "renta", "cobranza", "compras"):
            data: list[dict[str, Any]] = []
            for m in hist_months[-CHART_HISTORY:]:
                data.append({"label": month_label(m), "real": round(values[hist_months.index(m)], 2), "pronostico": None})
            data[-1]["pronostico"] = data[-1]["real"]  # une visualmente la línea real con la proyectada
            for i, m in enumerate(horizon):
                row = {"label": month_label(m), "real": None, "pronostico": round(p[i], 2)}
                if m == current:
                    row["real"] = round(series.get(m, 0.0), 2)  # parcial a la fecha
                data.append(row)
            charts.append(Chart(key=f"pron_{key}", title=f"{label}: real y pronóstico a {HORIZON} meses", type="line",
                                series=[ChartSeries(key="real", label="Real"),
                                        ChartSeries(key="pronostico", label="Pronóstico")], data=data))

    # ---------- flujo: cobros y pagos programados por vencimiento
    if ar is not None and ap is not None:
        ar_m, ar_overdue = ar
        ap_m, ap_overdue = ap
        cards += [
            card("cxc_programada_90", "CxC que vence en 90 días", sum(ar_m.get(m, 0.0) for m in horizon[:3]),
                 hint=f"Además hay ${ar_overdue:,.0f} ya vencidos"),
            card("cxp_programada_90", "CxP que vence en 90 días", sum(ap_m.get(m, 0.0) for m in horizon[:3]),
                 higher_is_better=False, hint=f"Además hay ${ap_overdue:,.0f} ya vencidos"),
        ]
        charts.append(Chart(
            key="calendario_flujo", title="Calendario de vencimientos: por cobrar vs por pagar", type="bar",
            series=[ChartSeries(key="cxc", label="CxC por vencer"), ChartSeries(key="cxp", label="CxP por vencer")],
            data=[{"label": "Ya vencido", "cxc": round(ar_overdue, 2), "cxp": round(ap_overdue, 2)}] +
                 [{"label": month_label(m), "cxc": round(ar_m.get(m, 0.0), 2), "cxp": round(ap_m.get(m, 0.0), 2)}
                  for m in horizon]))
    if contracts is not None:
        charts.append(Chart(
            key="contratos_vencen_mes", title="Contratos de renta en curso que vencen por mes", type="bar", unit="count",
            series=[ChartSeries(key="valor", label="Contratos")],
            data=[{"label": month_label(m), "valor": contracts.get(m, (0, 0.0))[0]} for m in horizon]))

    # ---------- tabla mensual
    rows = []
    for i, m in enumerate(horizon):
        row: dict[str, Any] = {"label": month_label(m)}
        for key, _label, _ in SERIES:
            if key in preds:
                v, mape = preds[key][i], mapes.get(key)
                row[key] = round(v, 2)
                if key in ("ventas", "renta", "cobranza"):
                    row[f"{key}_min"] = round(v * (1 - mape / 100), 2) if mape is not None else None
                    row[f"{key}_max"] = round(v * (1 + mape / 100), 2) if mape is not None else None
        if ar is not None:
            row["cxc_vence"] = round(ar[0].get(m, 0.0), 2)
        if ap is not None:
            row["cxp_vence"] = round(ap[0].get(m, 0.0), 2)
        if contracts is not None:
            row["contratos_vencen"] = contracts.get(m, (0, 0.0))[0]
            row["contratos_monto"] = round(contracts.get(m, (0, 0.0))[1], 2)
        rows.append(row)
    if rows:  # fila de total: la IA no debe sumar por su cuenta
        total_row: dict[str, Any] = {"label": f"Total {HORIZON} meses"}
        for k in rows[0]:
            if k != "label":
                vals = [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]
                total_row[k] = round(sum(vals), 2) if vals else None
        rows.append(total_row)
    columns = [Column(key="label", label="Mes")]
    labels = {"ventas": "Ventas", "renta": "Renta", "facturado": "Facturado", "cobranza": "Cobranza",
              "compras": "Compras", "reparacion": "Reparación"}
    for key, _label, _ in SERIES:
        if key not in preds:
            continue
        columns.append(Column(key=key, label=f"{labels[key]} (pron.)", type="money"))
        if key in ("ventas", "renta", "cobranza") and mapes.get(key) is not None:
            columns += [Column(key=f"{key}_min", label=f"{labels[key]} mín", type="money"),
                        Column(key=f"{key}_max", label=f"{labels[key]} máx", type="money")]
    if ar is not None:
        columns.append(Column(key="cxc_vence", label="CxC que vence", type="money"))
    if ap is not None:
        columns.append(Column(key="cxp_vence", label="CxP que vence", type="money"))
    if contracts is not None:
        columns += [Column(key="contratos_vencen", label="Contratos que vencen", type="number"),
                    Column(key="contratos_monto", label="Monto de esos contratos", type="money")]
    tables = [Table(key="pronostico_mensual", title=f"Pronóstico mensual ({month_label(horizon[0])} – "
                                                    f"{month_label(horizon[-1])})", columns=columns, rows=rows)]
    return cards, charts, tables
