"""Comisiones de venta — Fase 01 (requerimiento OdooV15.PROSIC.Sales_commissions, 22/05/2023).

Fuente: apuntes contables publicados (account.move.line) de las «cuentas 400 para ingresos NETOS cobrados»
que definió Finanzas (``COMMISSION_INCOME_ACCOUNTS`` y ``COMMISSION_CREDIT_NOTE_ACCOUNTS`` en odoo_mapping).

Base de comisión de un mes = ingreso neto (factura − notas de crédito, por línea y por cuenta) de las facturas
que quedaron COBRADAS AL 100 % en ese mes, con estas reglas del documento:
- Cobro con dinero: bancos, caja, compensaciones y pasarelas (PayPal, Stripe, Mercado Pago…), o
  aplicación de anticipo (nota de crédito contra la cuenta de anticipos de clientes 200-206). Odoo marca
  esas facturas como «Revertido» en lugar de «Pagado»: aquí se reconocen solas.
- No cuentan: cancelaciones al 100 % con nota de crédito, ni facturas saldadas con póliza del Diario de
  operaciones varias (Odoo las muestra «Pagadas»).
- Emisión no mayor a 365 días al momento del cobro.
- Cada factura cuenta UNA vez: en el mes en que quedó saldada (nunca en meses anteriores ni posteriores).
Solo lectura sobre Odoo.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from fnmatch import fnmatch
from typing import Any

from .. import odoo_mapping as M
from ..schemas.kpis import Chart, ChartSeries, Column, KpiCard, Table
from .common import Ctx, ModuleUnavailable, card, m2o_id, m2o_label, num, periods

Result = tuple[list[KpiCard], list[Chart], list[Table]]
TOL = 1.0  # pesos de tolerancia por redondeos

REASONS = {
    "nc_100": "Cancelada con nota de crédito (100 %)",
    "poliza": "Saldada con póliza de diario (cancelación / ajuste, no es cobro)",
    "mixta": "Saldada en parte con póliza de diario: revisar",
    "mas_365": "Cobrada más de 365 días después de la emisión",
}
KIND_LABEL = {"dinero": "Dinero", "anticipo": "Aplicación de anticipo", "nc": "Nota de crédito",
              "poliza": "Póliza de diario", "neutral": "Diferencia cambiaria"}


async def _accounts(ctx: Ctx) -> dict[str, Any]:
    """Cuentas por patrón de código (se resuelve en Python: no depende de operadores especiales de Odoo)."""
    accs = await ctx.odoo.search_read("account.account", [], ["code", "name", "internal_type"], limit=20000)

    def match(patterns: list[str]) -> set[int]:
        return {a["id"] for a in accs if any(fnmatch(a["code"] or "", p.replace("%", "*")) for p in patterns)}
    income = match(M.COMMISSION_INCOME_ACCOUNTS + M.COMMISSION_CREDIT_NOTE_ACCOUNTS)
    return {"income": income,
            "advance": match(M.CUSTOMER_ADVANCE_ACCOUNTS),
            "receivable": {a["id"] for a in accs if a.get("internal_type") == "receivable"},
            "names": {a["id"]: f"{a['code']} {a.get('name') or ''}".strip() for a in accs if a["id"] in income}}


def _branch_domain(ctx: Ctx, prefix: str = "") -> list[Any]:
    ids = ctx.branch_ids()
    return [(f"{prefix}branch_office_id", "in", ids)] if ids else []


async def _moves(ctx: Ctx, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = await ctx.odoo.search_read(
        "account.move", [("id", "in", sorted(ids))],
        ["name", "move_type", "invoice_date", "date", "ref", "partner_id", "invoice_user_id", "branch_office_id",
         "payment_state", "amount_total_signed", "amount_residual_signed", "journal_id", "state"], limit=len(ids))
    return {r["id"]: r for r in rows}


async def _income_lines(ctx: Ctx, accs: dict[str, Any], domain: list[Any]) -> list[dict[str, Any]]:
    return await ctx.odoo.search_read(
        "account.move.line",
        [("parent_state", "=", "posted"), ("account_id", "in", sorted(accs["income"]))] + domain,
        ["date", "move_id", "partner_id", "account_id", "debit", "credit", "cost_center_id", "product_id"],
        order="date, id", limit=100000)


def _net(l: dict[str, Any]) -> float:
    return num(l["credit"]) - num(l["debit"])


# --------------------------------------------------------------------- base de comisión
async def collected(ctx: Ctx, accs: dict[str, Any], period: tuple[date, date]) -> dict[str, Any]:
    """Facturas con ingresos de comisión que quedaron saldadas en el periodo (incluidas / excluidas con motivo)."""
    empty: dict[str, Any] = {"included": [], "excluded": [], "by_account": {}}
    d0, d1 = period[0].isoformat(), period[1].isoformat()
    partials = await ctx.odoo.search_read(
        "account.partial.reconcile",
        [("max_date", ">=", d0), ("max_date", "<=", d1), ("debit_move_id.move_id.move_type", "=", "out_invoice")],
        ["debit_move_id"], limit=100000)
    recv_ids = {m2o_id(p["debit_move_id"]) for p in partials}
    if not recv_ids:
        return empty
    recv = await ctx.odoo.search_read("account.move.line", [("id", "in", sorted(recv_ids))], ["move_id"], limit=len(recv_ids))
    inv_ids = {m2o_id(r["move_id"]) for r in recv}

    # solo facturas con ingresos de las cuentas de comisión (y de la sucursal filtrada)
    lines = await _income_lines(ctx, accs, [("move_id", "in", sorted(inv_ids))] + _branch_domain(ctx, "move_id."))
    inv_ids = {m2o_id(l["move_id"]) for l in lines}
    if not inv_ids:
        return empty
    inv = await _moves(ctx, inv_ids)

    # TODO lo que saldó cada factura (también pagos de meses anteriores): cuentas por cobrar de la factura
    rlines = await ctx.odoo.search_read(
        "account.move.line", [("move_id", "in", sorted(inv_ids)), ("account_id", "in", sorted(accs["receivable"]))],
        ["move_id", "matched_credit_ids"], limit=100000)
    pids = sorted({p for r in rlines for p in r["matched_credit_ids"]})
    parts = await ctx.odoo.search_read("account.partial.reconcile", [("id", "in", pids)],
                                       ["debit_move_id", "credit_move_id", "amount", "max_date"], limit=len(pids) or 1)
    line_to_inv = {r["id"]: m2o_id(r["move_id"]) for r in rlines}
    cp_ids = sorted({m2o_id(p["credit_move_id"]) for p in parts})
    cps = {c["id"]: c for c in await ctx.odoo.search_read(
        "account.move.line", [("id", "in", cp_ids)], ["move_id", "journal_id"], limit=len(cp_ids) or 1)}
    cp_moves = await _moves(ctx, {m2o_id(c["move_id"]) for c in cps.values()})
    journals = {j["id"]: j for j in await ctx.odoo.search_read("account.journal", [], ["type", "code"], limit=1000)}
    refund_ids = {mid for mid, m in cp_moves.items() if m["move_type"] == "out_refund"}
    advance_refunds: set[int] = set()
    refund_by_acc: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    if refund_ids:
        for l in await ctx.odoo.search_read(
                "account.move.line", [("move_id", "in", sorted(refund_ids)),
                                      ("account_id", "in", sorted(accs["advance"] | accs["income"]))],
                ["move_id", "account_id", "debit", "credit"], limit=100000):
            mid, acc = m2o_id(l["move_id"]), m2o_id(l["account_id"])
            if acc in accs["advance"]:
                advance_refunds.add(mid)
            else:
                refund_by_acc[mid][acc] += _net(l)

    def kind(cp_line: dict[str, Any]) -> str:
        mid = m2o_id(cp_line["move_id"])
        if mid in refund_ids:
            return "anticipo" if mid in advance_refunds else "nc"
        j = journals.get(m2o_id(cp_line["journal_id"]) or 0, {})
        if j.get("type") in ("bank", "cash"):
            return "dinero"
        if j.get("code") in M.COMMISSION_NEUTRAL_JOURNALS:
            return "neutral"
        return "poliza"

    settle: dict[int, dict[str, Any]] = defaultdict(lambda: {"amounts": defaultdict(float), "last": "", "refunds": set(),
                                                              "kinds": set()})
    for p in parts:
        iid = line_to_inv.get(m2o_id(p["debit_move_id"]))
        cp = cps.get(m2o_id(p["credit_move_id"]))
        if iid is None or cp is None:
            continue
        k = kind(cp)
        s = settle[iid]
        s["amounts"][k] += num(p["amount"])
        s["kinds"].add(k)
        s["last"] = max(s["last"], p["max_date"] or "")
        if k in ("nc", "anticipo"):
            s["refunds"].add(m2o_id(cp["move_id"]))

    income_by_inv: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    cc_by_inv: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for l in lines:
        iid = m2o_id(l["move_id"])
        income_by_inv[iid][m2o_id(l["account_id"])] += _net(l)
        cc_by_inv[iid][m2o_label(l["cost_center_id"]) or "Sin centro de costo"] += _net(l)

    included, excluded = [], []
    by_account: dict[int, float] = defaultdict(float)
    for iid, m in inv.items():
        s = settle.get(iid)
        if not s or abs(num(m["amount_residual_signed"])) > TOL:
            continue  # aún tiene saldo: comisionará el mes en que se termine de cobrar
        if not (d0 <= s["last"] <= d1):
            continue  # se terminó de saldar en otro mes
        a = s["amounts"]
        accounts: dict[int, float] = defaultdict(float, income_by_inv[iid])
        for r in s["refunds"]:
            for acc, v in refund_by_acc[r].items():
                accounts[acc] += v
        income = sum(income_by_inv[iid].values())
        nc = sum(accounts.values()) - income
        cc = max(cc_by_inv[iid].items(), key=lambda kv: abs(kv[1]))[0]
        row = {"_id": iid, "factura": m["name"], "fecha_factura": m["invoice_date"] or m["date"],
               "fecha_cobro": s["last"], "cliente": m2o_label(m["partner_id"]),
               "vendedor": m2o_label(m["invoice_user_id"]) or "Sin vendedor",
               "sucursal": m2o_label(m["branch_office_id"]) or "Sin sucursal", "centro_costo": cc,
               "ingreso": round(income, 2), "nc": round(nc, 2), "base": round(income + nc, 2),
               "cobro": ", ".join(KIND_LABEL[k] for k in sorted(s["kinds"])), "estado_pago": m["payment_state"]}
        try:
            row["dias"] = (date.fromisoformat(s["last"]) - date.fromisoformat(row["fecha_factura"])).days
        except (TypeError, ValueError):
            row["dias"] = None
        cash = a["dinero"] + a["anticipo"]
        if cash <= TOL:
            reason = "nc_100" if a["nc"] > TOL and a["poliza"] <= TOL else "poliza"
        elif a["poliza"] > TOL:
            reason = "mixta"
        elif row["dias"] is not None and row["dias"] > M.COMMISSION_MAX_DAYS:
            reason = "mas_365"
        else:
            reason = None
        if reason:
            excluded.append({**row, "motivo": REASONS[reason]})
        else:
            included.append(row)
            for acc, v in accounts.items():
                by_account[acc] += v
    return {"included": included, "excluded": excluded, "by_account": by_account}


# --------------------------------------------------------------------- módulo
async def build(ctx: Ctx) -> Result:
    try:
        accs = await _accounts(ctx)
    except Exception as exc:  # noqa: BLE001
        raise ModuleUnavailable(f"No se pudieron leer las cuentas contables: {exc}") from exc
    if not accs["income"]:
        raise ModuleUnavailable("No se encontraron en Odoo las cuentas de ingreso para comisiones; revisa "
                                "COMMISSION_* en odoo_mapping.py")
    if ctx.filters.warehouse_id or ctx.filters.allowed_warehouse_ids:
        ctx.restrict("Comisiones", "almacén")
    cur, prev = periods(ctx.filters)
    if cur[1] < M.COMMISSION_START:
        raise ModuleUnavailable(f"El cálculo aplica desde {M.COMMISSION_START:%d/%m/%Y} (arranque de Odoo V15).")
    cur = (max(cur[0], M.COMMISSION_START), cur[1])

    res = await collected(ctx, accs, cur)
    res_prev = await collected(ctx, accs, prev) if prev and prev[1] >= M.COMMISSION_START else None
    detail = await _income_lines(ctx, accs, [("date", ">=", cur[0].isoformat()), ("date", "<=", cur[1].isoformat())]
                                 + _branch_domain(ctx, "move_id."))
    dmoves = await _moves(ctx, {m2o_id(l["move_id"]) for l in detail})

    inc, exc = res["included"], res["excluded"]
    base = sum(r["base"] for r in inc)
    base_prev = sum(r["base"] for r in res_prev["included"]) if res_prev else None
    billed = sum(_net(l) for l in detail)
    credit_notes = sum(_net(l) for l in detail if dmoves.get(m2o_id(l["move_id"]), {}).get("move_type") == "out_refund")
    pending = sum(abs(num(m["amount_residual_signed"])) for m in dmoves.values() if m["move_type"] == "out_invoice")

    cards = [
        card("base_comision", "Base de comisión (ingresos netos cobrados al 100 %)", base, base_prev,
             hint="Facturas saldadas en el periodo con dinero o anticipo, menos sus notas de crédito"),
        card("facturas_cobradas", "Facturas que comisionan", len(inc),
             len(res_prev["included"]) if res_prev else None, unit="count"),
        card("facturas_excluidas", "Facturas saldadas que NO comisionan", len(exc), unit="count", higher_is_better=False,
             hint="Cancelaciones, pólizas o cobro +365 días: ver tabla"),
        card("facturacion_comision", "Facturación del periodo en cuentas de comisión (neta)", billed,
             hint="Apuntes de las cuentas de comisión por fecha contable"),
        card("nc_comision", "Notas de crédito del periodo", credit_notes, higher_is_better=False),
        card("pendiente_comision", "Importe pendiente de las facturas del periodo", pending, higher_is_better=False,
             hint="Saldo por cobrar (con IVA) de las facturas emitidas en el periodo"),
    ]

    by_seller: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"facturas": 0, "base": 0.0})
    by_branch: dict[str, float] = defaultdict(float)
    for r in inc:
        k = (r["vendedor"], r["sucursal"])
        by_seller[k]["facturas"] += 1
        by_seller[k]["base"] += r["base"]
        by_branch[r["sucursal"]] += r["base"]
    seller_tot: dict[str, float] = defaultdict(float)
    for (v, _), d in by_seller.items():
        seller_tot[v] += d["base"]

    charts = [
        Chart(key="base_vendedor", title="Base de comisión por vendedor (top 15)", type="hbar", entity="vendedor",
              series=[ChartSeries(key="valor", label="Base")],
              data=[{"label": v, "valor": round(b, 2)} for v, b in sorted(seller_tot.items(), key=lambda kv: -kv[1])[:15]]),
        Chart(key="base_sucursal", title="Base de comisión por sucursal", type="hbar",
              series=[ChartSeries(key="valor", label="Base")],
              data=[{"label": s, "valor": round(b, 2)} for s, b in sorted(by_branch.items(), key=lambda kv: -kv[1])]),
    ]

    # Cuentas: base cobrada vs facturado del periodo, en el orden de la lista de Finanzas.
    billed_by_acc: dict[int, float] = defaultdict(float)
    for l in detail:
        billed_by_acc[m2o_id(l["account_id"])] += _net(l)
    acc_ids = sorted(set(res["by_account"]) | set(billed_by_acc), key=lambda i: accs["names"].get(i, ""))
    acc_rows = [{"cuenta": accs["names"].get(i, str(i)), "base": round(res["by_account"].get(i, 0.0), 2),
                 "facturado": round(billed_by_acc.get(i, 0.0), 2)} for i in acc_ids]
    acc_rows.append({"cuenta": "Total general", "base": round(base, 2), "facturado": round(billed, 2)})

    money = lambda k, l: Column(key=k, label=l, type="money")  # noqa: E731
    inv_cols = [Column(key="factura", label="Factura"), Column(key="fecha_factura", label="Fecha factura", type="date"),
                Column(key="fecha_cobro", label="Fecha de cobro (saldada)", type="date"),
                Column(key="dias", label="Días a cobro", type="number"),
                Column(key="cliente", label="Cliente", entity="cliente"),
                Column(key="vendedor", label="Vendedor", entity="vendedor"), Column(key="sucursal", label="Sucursal"),
                Column(key="centro_costo", label="Centro de costo"), money("ingreso", "Ingreso facturado"),
                money("nc", "Notas de crédito"), money("base", "Base comisión"), Column(key="cobro", label="Saldada con")]
    tables = [
        Table(key="base_cuenta", title="Base de comisión por cuenta contable (ingresos netos cobrados)",
              columns=[Column(key="cuenta", label="Cuenta"), money("base", "Base cobrada (comisiona)"),
                       money("facturado", "Facturado en el periodo")], rows=acc_rows),
        Table(key="base_vendedor_sucursal", title="Base de comisión por vendedor y sucursal",
              columns=[Column(key="vendedor", label="Vendedor", entity="vendedor"), Column(key="sucursal", label="Sucursal"),
                       Column(key="facturas", label="Facturas", type="number"), money("base", "Base comisión")],
              rows=[{"vendedor": v, "sucursal": s, "facturas": int(d["facturas"]), "base": round(d["base"], 2)}
                    for (v, s), d in sorted(by_seller.items(), key=lambda kv: (-kv[1]["base"]))]),
        Table(key="facturas_comisionan", title="Facturas cobradas al 100 % en el periodo (base de comisión)",
              columns=inv_cols, rows=sorted(inc, key=lambda r: (r["vendedor"], r["fecha_cobro"])),
              ai_include=False, odoo_model="account.move"),
        Table(key="facturas_excluidas", title="Facturas saldadas en el periodo que NO comisionan (para revisión)",
              columns=[*inv_cols[:8], money("base", "Ingreso neto"), Column(key="cobro", label="Saldada con"),
                       Column(key="motivo", label="Motivo")],
              rows=sorted(exc, key=lambda r: r["motivo"]), ai_include=False, odoo_model="account.move"),
        Table(key="detalle_facturacion", title="Detalle de facturación (Fase 01: apuntes de las cuentas de comisión)",
              ai_include=False, odoo_model="account.move",
              columns=[Column(key="fecha", label="Fecha", type="date"), Column(key="cliente", label="Cliente", entity="cliente"),
                       Column(key="referencia", label="Referencia"), Column(key="asiento", label="Asiento"),
                       Column(key="estado", label="Estado"), Column(key="cuenta", label="Cuenta"),
                       money("debito", "Débito"), money("credito", "Crédito"), money("neto", "Neto"),
                       Column(key="vendedor", label="Vendedor", entity="vendedor"), Column(key="sucursal", label="Sucursal"),
                       Column(key="centro_costo", label="Centro de costo"), Column(key="estado_pago", label="Estado de pago")],
              rows=[_detail_row(l, dmoves.get(m2o_id(l["move_id"]), {})) for l in detail]),
    ]
    ctx.note("Cuentas de comisión (lista de Finanzas): " + ", ".join(M.COMMISSION_INCOME_ACCOUNTS + M.COMMISSION_CREDIT_NOTE_ACCOUNTS)
             + ". La factura comisiona en el mes en que queda saldada al 100 % con dinero (bancos, caja, compensaciones, "
             "pasarelas) o con aplicación de anticipo, si no pasaron más de 365 días desde su emisión.")
    ctx.note("Las facturas con anticipo aplicado (Odoo las marca «Revertido») y las canceladas con póliza de diario "
             "(Odoo las marca «Pagado») se clasifican automáticamente; revisa la tabla de excluidas.")
    if ctx.filters.branch_id or ctx.filters.allowed_branch_ids:
        ctx.note("Filtrado por la sucursal de la factura.")
    return cards, charts, tables


STATE_ES = {"posted": "Publicado", "draft": "Borrador", "cancel": "Cancelado"}
PAY_ES = {"not_paid": "Sin pagar", "in_payment": "En proceso de pago", "paid": "Pagado", "partial": "Pagado parcialmente",
          "reversed": "Revertido", "invoicing_legacy": "Sistema anterior"}


def _detail_row(l: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    d, c = num(l["debit"]), num(l["credit"])
    return {"_id": m2o_id(l["move_id"]), "fecha": l["date"], "cliente": m2o_label(l["partner_id"]),
            "referencia": m.get("ref") or "", "asiento": m2o_label(l["move_id"]),
            "estado": STATE_ES.get(m.get("state", "posted"), m.get("state")), "cuenta": m2o_label(l["account_id"]),
            "debito": round(d, 2), "credito": round(c, 2), "neto": round(c - d, 2),
            "vendedor": m2o_label(m.get("invoice_user_id")) or "", "sucursal": m2o_label(m.get("branch_office_id")) or "",
            "centro_costo": m2o_label(l["cost_center_id"]) or "",
            "estado_pago": PAY_ES.get(m.get("payment_state") or "", m.get("payment_state") or "")}
