"""Panel inteligente de comisiones: de lo cobrado a la comisión, qué revisar, comisión potencial y ficha por vendedor.

Usa los mismos datos que el módulo Comisiones (Fase 01 en caché hasta la corrida nocturna) y el plan local, así que
abrir el panel no vuelve a consultar Odoo. La única consulta propia es la CARTERA ABIERTA (facturas de cliente
publicadas que aún no se cobran al 100 %), con dos llamadas agrupadas a Odoo y en caché hasta la corrida nocturna.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .. import odoo_mapping as M
from .commission_plan import (Plan, base_for, calculate, depurar, get_plan_store, month_label, puesto_cuentas, tramo)
from .common import Ctx, m2o_id, m2o_label, num
from .comisiones import _accounts, _branch_domain, _by_code, _moves, _phase01_cached, local_today, occupancy_fn

RISK_WINDOW = 60  # días antes de cumplir el máximo para avisar
NEAR_TIER = 0.15  # avisar si al vendedor le falta menos de este % de su meta para subir de tramo
TOP = 8


# ------------------------------------------------------------------ cartera abierta (comisión potencial)
async def open_invoices(ctx: Ctx) -> list[dict[str, Any]]:
    """Facturas de cliente publicadas con saldo, con su ingreso por cuenta de comisión (una fila por factura)."""
    from ..cache import make_key
    from .registry import seconds_until_sync

    key = make_key("comis-abiertas", ctx.branch_ids())
    cache = getattr(ctx, "cache", None)
    if cache is not None:
        hit = await cache.get_json(key)
        if hit is not None:
            return hit
    accs = await _accounts(ctx)
    dom = [("parent_state", "=", "posted"), ("account_id", "in", sorted(accs["income"])),
           ("move_id.move_type", "=", "out_invoice"), ("move_id.payment_state", "in", ["not_paid", "partial"]),
           ("date", ">=", M.COMMISSION_START.isoformat())] + _branch_domain(ctx, "move_id.")
    groups = await ctx.odoo.read_group("account.move.line", dom, ["credit:sum", "debit:sum"],
                                       ["move_id", "account_id", "cost_center_id"])
    by_inv: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    cc: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in groups:
        mid, acc = m2o_id(g.get("move_id")), m2o_id(g.get("account_id"))
        if mid is None or acc is None:
            continue
        net = num(g.get("credit")) - num(g.get("debit"))
        by_inv[mid][acc] += net
        cc[mid][m2o_label(g.get("cost_center_id"))] += net
    moves = await _moves(ctx, set(by_inv))
    today = local_today(ctx)
    out = []
    for mid, m in moves.items():
        income = by_inv[mid]
        inv_date = m.get("invoice_date") or m.get("date")
        try:
            dias = (today - date.fromisoformat(inv_date)).days
        except (TypeError, ValueError):
            dias = None
        total = abs(num(m.get("amount_total_signed")))
        out.append({
            "_id": mid, "factura": m["name"], "fecha_factura": inv_date, "vence": m.get("invoice_date_due"),
            "fecha_cobro": today.isoformat(), "dias": dias, "cliente": m2o_label(m["partner_id"]),
            "vendedor": m2o_label(m["invoice_user_id"]) or "Sin vendedor", "vendedor_id": m2o_id(m["invoice_user_id"]),
            "sucursal": m2o_label(m["branch_office_id"]) or "Sin sucursal", "sucursal_id": m2o_id(m["branch_office_id"]),
            "centro_costo": max(cc[mid].items(), key=lambda kv: abs(kv[1]))[0] if cc[mid] else "Sin centro de costo",
            "base": round(sum(income.values()), 2), "cuentas": _by_code(accs, income), "ingreso": _by_code(accs, income),
            "saldo": round(abs(num(m.get("amount_residual_signed"))), 2),
            "cobrado_pct": round((1 - abs(num(m.get("amount_residual_signed"))) / total) * 100, 1) if total else 0.0,
        })
    if cache is not None and ctx.settings is not None:
        await cache.set_json(key, out, max(seconds_until_sync(ctx.settings), ctx.settings.KPI_CACHE_TTL_SECONDS))
    return out


# ------------------------------------------------------------------ panel
def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _seller_pct(plan: Plan, vid: int | None, last_row: dict[str, Any] | None) -> float:
    """% de comisión para estimar la cartera: el del último mes calculado o, si no hay, el primer tramo del puesto."""
    if last_row and last_row.get("pct_comision"):
        return float(last_row["pct_comision"])
    v = plan.vendedor(vid) if vid else None
    p = plan.puesto(v.puesto) if v else None
    if v and v.activo and p and p.tramos:
        return p.tramos[0].valor
    return 0.0


def next_tier(plan: Plan, row: dict[str, Any]) -> dict[str, Any] | None:
    """Cuánto le falta al vendedor para el siguiente tramo del tabulador y cuánto ganaría de más."""
    v = plan.vendedor(row.get("vendedor_id"))
    p = plan.puesto(v.puesto) if v else None
    meta, cump = row.get("meta"), row.get("cumplimiento")
    if not (p and meta and cump is not None):
        return None
    nxt = next((t for t in sorted(p.tramos, key=lambda t: t.desde) if t.desde > cump + 1e-9), None)
    if not nxt:
        return None
    falta = meta * nxt.desde / 100 - row["base"]
    factor = row.get("factor") if row.get("factor") is not None else 1.0
    extra = ((row["base"] + falta) * nxt.valor / 100 - row["base"] * (row.get("pct_comision") or 0) / 100) * factor
    return {"desde": nxt.desde, "valor": nxt.valor, "falta_base": round(max(falta, 0), 2), "comision_extra": round(extra, 2)}


async def build_panel(ctx: Ctx) -> dict[str, Any]:
    settings = ctx.settings
    f1 = await _phase01_cached(ctx)
    months: list[str] = f1["calc_months"]
    store = get_plan_store(settings.HISTORY_DB_PATH if settings else "")
    plan = store.plan()
    data = {m: store.month(m) for m in months}
    today = local_today(ctx)
    clean = await asyncio.to_thread(depurar, plan, f1["calc_invoices"])
    rows = await asyncio.to_thread(calculate, plan, data, f1["calc_invoices"], occupancy_fn(ctx), today, clean)
    try:
        opened = await open_invoices(ctx)
        open_ok = True
    except Exception as exc:  # noqa: BLE001 — el panel sigue sin la cartera abierta
        ctx.note(f"Comisión potencial no disponible: {exc}")
        opened, open_ok = [], False
    open_clean = await asyncio.to_thread(depurar, plan, opened)
    allowed = set(ctx.branch_ids())
    if allowed:
        rows = [r for r in rows if r["sucursal_id"] in allowed]
        clean = [r for r in clean if r.get("sucursal_id") in allowed or r.get("sucursal_original_id") in allowed]
        open_clean = [r for r in open_clean if r.get("sucursal_id") in allowed]
    dep = plan.depuracion

    # ---------- de lo cobrado a la comisión (meses completos del cálculo)
    total = sum(r["base"] * r["_share"] for r in clean)
    auto = sum(r["base"] * r["_share"] for r in clean if r["estado"] != "Comisiona" and not r["motivo"].startswith("Regla:"))
    by_rule = sum(r["base"] * r["_share"] for r in clean if r["motivo"].startswith("Regla:"))
    ok = [r for r in clean if r["estado"] == "Comisiona"]
    base_ok = sum(r["base_comisionable"] for r in ok)
    other_acc = sum(r["base"] * r["_share"] for r in ok) - base_ok
    comision = sum(r["comision"] for r in rows)
    accounts_txt = ", ".join(dep.cuentas) if dep.cuentas else "todas las cuentas de comisión"
    embudo = [
        {"key": "saldado", "label": "Facturas cobradas al 100 %", "monto": _r(total), "tipo": "total",
         "detalle": f"{len({r['factura'] for r in clean})} facturas con ingreso, sin IVA"},
        {"key": "auto", "label": "Exclusiones automáticas", "monto": _r(-auto), "tipo": "resta",
         "detalle": f"Notas de crédito al 100 %, pólizas de diario o cobro después de {dep.max_dias} días"},
        {"key": "reglas", "label": "Excluidas por reglas", "monto": _r(-by_rule), "tipo": "resta",
         "detalle": f"{len([x for x in dep.reglas if x.activo and x.accion == 'excluir'])} reglas de exclusión activas"},
        {"key": "cuentas", "label": "Ingresos de cuentas que no comisionan", "monto": _r(-other_acc), "tipo": "resta",
         "detalle": f"Solo comisiona: {accounts_txt}"},
        {"key": "base", "label": "Base comisionable", "monto": _r(base_ok), "tipo": "resultado",
         "detalle": f"{len(ok)} facturas"},
        {"key": "comision", "label": "Comisión calculada", "monto": _r(comision), "tipo": "comision",
         "detalle": f"{(comision / base_ok * 100):.2f} % efectivo sobre la base" if base_ok else ""},
    ]

    # ---------- por vendedor
    last_month = months[-1] if months else None
    by_seller: dict[Any, dict[str, Any]] = {}
    for r in rows:
        s = by_seller.setdefault(r["vendedor_id"], {
            "id": r["vendedor_id"], "nombre": r["vendedor"], "puesto": r["puesto"], "sucursal": r["sucursal"],
            "base_tipo": r["base_tipo"], "base": 0.0, "comision": 0.0, "facturas": 0, "ajustes": 0.0,
            "meses": [], "observaciones": [], "ultimo": None})
        s["base"] += r["base"]
        s["comision"] += r["comision"]
        s["facturas"] += r["facturas"]
        s["ajustes"] += r["ajustes"]
        s["meses"].append({"mes": r["mes"], "label": r["mes_label"], "base": r["base"], "comision": r["comision"]})
        for o in [x for x in (r["observaciones"] or "").split("; ") if x]:
            if o not in s["observaciones"]:
                s["observaciones"].append(o)
        if r["mes"] == last_month:
            s["ultimo"] = r
    # cartera abierta por beneficiario (con las reglas de reasignación aplicadas)
    pot: dict[Any, dict[str, Any]] = defaultdict(lambda: {"base": 0.0, "riesgo": 0.0, "perdida": 0.0, "facturas": []})
    branch_pot: dict[Any, float] = defaultdict(float)
    for r in open_clean:
        vid = r.get("vendedor_id")
        v = plan.vendedor(vid)
        b = base_for(r, puesto_cuentas(plan, plan.puesto(v.puesto) if v else None), dep.notas_credito)
        if b <= 0:
            continue
        d = r.get("dias")
        if r["estado"] != "Comisiona":
            if d is not None and d > dep.max_dias:
                pot[vid]["perdida"] += b
            continue
        pot[vid]["base"] += b
        branch_pot[r.get("sucursal_id")] += b
        if d is not None and d >= dep.max_dias - RISK_WINDOW:
            pot[vid]["riesgo"] += b
        pot[vid]["facturas"].append({"factura": r["factura"], "cliente": r["cliente"], "fecha_factura": r["fecha_factura"],
                                     "dias": d, "base": round(b, 2), "saldo": r["saldo"], "cobrado_pct": r["cobrado_pct"],
                                     "limite": (date.fromisoformat(r["fecha_factura"]) + timedelta(days=dep.max_dias)).isoformat()
                                     if r.get("fecha_factura") else None})
    for vid, p in pot.items():
        if vid not in by_seller and p["base"] > 0:
            v = plan.vendedor(vid)
            by_seller[vid] = {"id": vid, "nombre": v.nombre if v else next(
                (r["vendedor"] for r in open_clean if r.get("vendedor_id") == vid), "Sin vendedor"),
                "puesto": "—", "sucursal": "—", "base_tipo": "Propia", "base": 0.0, "comision": 0.0, "facturas": 0,
                "ajustes": 0.0, "meses": [], "observaciones": [] if v else ["Vendedor no configurado en el plan"], "ultimo": None}
            if v:
                pp = plan.puesto(v.puesto)
                by_seller[vid]["puesto"] = pp.label if pp else "—"
    sellers = []
    for vid, s in by_seller.items():
        v = plan.vendedor(vid)
        p = plan.puesto(v.puesto) if v else None
        last = s.pop("ultimo")
        pct = _seller_pct(plan, vid, last)
        factor = last.get("factor") if last and last.get("factor") is not None else 1.0
        if p and p.base == "sucursal" and v and v.sucursal_id is not None:
            pot_base = branch_pot.get(v.sucursal_id, 0.0)
        else:
            pot_base = pot[vid]["base"] if vid in pot else 0.0
        mine = sorted((r for r in ok if r.get("vendedor_id") == vid), key=lambda r: -r["base_comisionable"])
        open_list = sorted(pot[vid]["facturas"], key=lambda f: -f["base"]) if vid in pot else []
        sellers.append({
            **{k: (round(x, 2) if isinstance(x, float) else x) for k, x in s.items()},
            "activo": bool(v and v.activo), "en_plan": v is not None,
            "pct": pct, "factor": factor,
            "meta": last.get("meta") if last else None, "cumplimiento": last.get("cumplimiento") if last else None,
            "base_mes": last.get("base") if last else 0.0, "comision_mes": last.get("comision") if last else 0.0,
            "pct_mes": last.get("pct_comision") if last else pct,
            "tramos": [t.model_dump() for t in p.tramos] if p else [],
            "siguiente": next_tier(plan, last) if last else None,
            "potencial_base": round(pot_base, 2), "potencial_comision": round(pot_base * pct / 100 * factor, 2),
            "riesgo_base": round(pot[vid]["riesgo"], 2) if vid in pot else 0.0,
            "riesgo_comision": round((pot[vid]["riesgo"] if vid in pot else 0.0) * pct / 100 * factor, 2),
            "perdida_base": round(pot[vid]["perdida"], 2) if vid in pot else 0.0,
            "facturas_top": [{"factura": r["factura"], "cliente": r["cliente"], "fecha_cobro": r["fecha_cobro"],
                              "base": round(r["base_comisionable"], 2),
                              "traspaso": r.get("vendedor_original") if r.get("vendedor_original_id") != vid else None,
                              "regla": r.get("regla") or None} for r in mine[:TOP]],
            "abiertas_top": open_list[:TOP], "abiertas": len(open_list),
        })
    sellers.sort(key=lambda s: (-s["comision"], -s["potencial_comision"], s["nombre"]))

    # ---------- qué revisar
    alerts: list[dict[str, Any]] = []

    def alert(nivel: str, titulo: str, detalle: str, monto: float | None = None, vendedores: list[Any] | None = None,
              accion: str | None = None) -> None:
        alerts.append({"nivel": nivel, "titulo": titulo, "detalle": detalle, "monto": _r(monto),
                       "vendedores": [x for x in (vendedores or []) if x is not None][:12], "accion": accion})

    names = {s["id"]: s["nombre"] for s in sellers}

    def who(ids: list[Any], n: int = 3) -> str:
        ns = [names.get(i, "Sin vendedor") for i in ids]
        return ", ".join(ns[:n]) + (f" y {len(ns) - n} más" if len(ns) > n else "")

    risk = [s for s in sellers if s["riesgo_base"] > 0]
    if risk:
        rb = sum(s["riesgo_base"] for s in risk)
        rc = sum(s["riesgo_comision"] for s in risk)
        risk.sort(key=lambda s: -s["riesgo_base"])
        alert("riesgo", f"Comisión en riesgo: ${rc:,.0f}",
              f"Facturas por ${rb:,.0f} de base cumplen {dep.max_dias} días en los próximos {RISK_WINDOW} sin cobrarse; si no se "
              f"cobran antes, ya no comisionan. Mayor monto: {who([s['id'] for s in risk])}.", rc, [s["id"] for s in risk], "cobranza")
    lost = [s for s in sellers if s["perdida_base"] > 0]
    if lost:
        lb = sum(s["perdida_base"] for s in lost)
        alert("info", f"Cartera vieja que ya no comisiona: ${lb:,.0f}",
              f"Facturas abiertas con más de {dep.max_dias} días: aunque se cobren, no generan comisión (salvo una regla «Incluir»).",
              lb, [s["id"] for s in sorted(lost, key=lambda s: -s["perdida_base"])], "reglas")
    outside = [s for s in sellers if not s["en_plan"] and s["id"] is not None and (s["base"] or s["potencial_base"])]
    if outside:
        ob = sum(s["base"] for s in outside)
        alert("revisar", f"{len(outside)} vendedor(es) de Odoo fuera del plan",
              f"{who([s['id'] for s in outside])} tienen base cobrada (${ob:,.0f}) o cartera, pero no están en el plan: "
              "agrégalos en Administración → Comisiones, o traspasa sus facturas con una regla «Reasignar».",
              ob, [s["id"] for s in outside], "plan")
    no_seller = next((s for s in sellers if s["id"] is None and s["base"]), None)
    if no_seller:
        alert("revisar", "Facturas sin vendedor en Odoo",
              f"{no_seller['facturas']} facturas (${no_seller['base']:,.0f}) no tienen vendedor: corrígelas en Odoo o "
              "asígnalas con una regla «Reasignar» (por cliente o folio).", no_seller["base"], None, "reglas")
    inactive = [s for s in sellers if s["en_plan"] and not s["activo"] and (s["base"] or s["potencial_base"])]
    if inactive:
        alert("revisar", f"{len(inactive)} vendedor(es) inactivo(s) con cobranza",
              f"{who([s['id'] for s in inactive])} ya no están activos pero siguen generando base o tienen cartera: "
              "traspasa sus facturas a quien atiende ahora a esos clientes.", sum(s["base"] + s["potencial_base"] for s in inactive),
              [s["id"] for s in inactive], "reglas")
    for label, key in (("Falta meta del mes", "meta"), ("Falta tabulador del puesto", "tabulador"),
                       ("Cumplimiento debajo del primer tramo", "tramo")):
        hit = [s for s in sellers if label in s["observaciones"]]
        if hit:
            alert("revisar", f"{label}: {len(hit)} vendedor(es)", f"{who([s['id'] for s in hit], 4)}.", None,
                  [s["id"] for s in hit], "metas" if key == "meta" else "plan")
    manual_rows = [r for r in rows if r.get("manual") is not None]
    if manual_rows:
        diff = sum(r["diferencia"] for r in manual_rows)
        big = sorted((r for r in manual_rows if abs(r["diferencia"]) >= max(1.0, abs(r["manual"]) * 0.01)),
                     key=lambda r: -abs(r["diferencia"]))
        if big:
            alert("revisar", f"Diferencias con el cálculo de Finanzas: {len(big)} vendedor(es)",
                  f"La diferencia total es ${diff:,.0f} (positivo = el BI calcula más). Mayor diferencia: "
                  f"{who([r['vendedor_id'] for r in big])}.", diff, [r["vendedor_id"] for r in big], "metas")
        else:
            alert("ok", "El cálculo coincide con Finanzas", f"{len(manual_rows)} vendedores comparados sin diferencias de más de 1 %.")
    mixed = [r for r in clean if r.get("motivo_key") == "mixta"]
    if mixed:
        alert("revisar", f"{len(mixed)} factura(s) saldadas en parte con póliza",
              "No comisionan hasta revisarlas: si la póliza fue un cobro real, inclúyelas con una regla «Incluir» por folio.",
              sum(r["base"] for r in mixed), None, "reglas")
    near = [s for s in sellers if s["siguiente"] and s["meta"] and s["siguiente"]["falta_base"] <= s["meta"] * NEAR_TIER]
    for s in sorted(near, key=lambda s: s["siguiente"]["falta_base"])[:5]:
        n = s["siguiente"]
        alert("oportunidad", f"{s['nombre']} está cerca del siguiente tramo",
              f"Le faltan ${n['falta_base']:,.0f} de cobranza para pasar de {s['pct_mes']} % a {n['valor']} % "
              f"(+${n['comision_extra']:,.0f} de comisión).", n["comision_extra"], [s["id"]])
    pot_total = sum(s["potencial_comision"] for s in sellers)  # incluye a los gerentes: su comisión se suma
    pot_base_total = sum(r["base_comisionable"] for r in open_clean if r["estado"] == "Comisiona")
    if not [a for a in alerts if a["nivel"] in ("riesgo", "revisar")]:
        alert("ok", "Sin pendientes de revisión", "Todos los vendedores con cobranza están en el plan, con tabulador y sin observaciones.")

    order = {"riesgo": 0, "revisar": 1, "oportunidad": 2, "info": 3, "ok": 4}
    alerts.sort(key=lambda a: order[a["nivel"]])
    return {
        "meses": [{"mes": m, "label": month_label(m)} for m in months],
        "preliminar": bool(months) and months[-1] >= today.strftime("%Y-%m"),
        "cuentas": dep.cuentas, "max_dias": dep.max_dias,
        "resumen": {
            "comision": _r(comision), "base_comisionable": _r(base_ok), "vendedores": len([s for s in sellers if s["comision"] > 0]),
            "potencial_base": _r(pot_base_total), "potencial_comision": _r(pot_total),
            "riesgo_base": _r(sum(s["riesgo_base"] for s in sellers)),
            "riesgo_comision": _r(sum(s["riesgo_comision"] for s in sellers)),
            "cartera_disponible": open_ok,
        },
        "embudo": embudo, "alertas": alerts, "vendedores": sellers, "notas": ctx.notes,
    }
