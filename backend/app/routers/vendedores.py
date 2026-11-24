"""Vendedores: ranking del periodo y ficha de cada vendedor (pedidos, facturado, clientes, cartera y comisión).

Reutiliza los cálculos del módulo Ventas limitados a los pedidos (``user_id``) y facturas (``invoice_user_id``) del
vendedor. Misma caché que los módulos: vigente hasta la actualización nocturna de Odoo.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from .. import odoo_mapping as M
from ..auth import User, current_user, require_module
from ..cache import Cache, make_key
from ..config import Settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError
from ..schemas.kpis import Column, CompareMode, KpiFilters, ModuleKpis, Period, Table
from ..services import ventas
from ..services.common import Ctx, ModuleUnavailable, ScopeRestricted, build_domain, m2o_label, num, periods
from ..services.history import get_history
from ..services.registry import seconds_until_sync

router = APIRouter(prefix="/api/vendedores", tags=["vendedores"])
PAY_ES = {"not_paid": "Sin pagar", "in_payment": "En proceso de pago", "paid": "Pagada", "partial": "Pago parcial",
          "reversed": "Revertida", "invoicing_legacy": "Sistema anterior"}


def parse(date_from: date = Query(...), date_to: date = Query(...), company_id: int | None = Query(None, ge=1),
          branch_id: int | None = Query(None, ge=1), compare: CompareMode = Query("previous"),
          iva: Literal["sin", "con"] = Query("sin")) -> KpiFilters:
    try:
        return KpiFilters(date_from=date_from, date_to=date_to, company_id=company_id, branch_id=branch_id,
                          compare=compare, iva=iva)
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from exc


def _ctx(odoo: OdooClient, cache: Cache, settings: Settings, filters: KpiFilters, seller_id: int | None = None) -> Ctx:
    return Ctx(odoo=odoo, filters=filters, tz=settings.ODOO_TZ, history=get_history(settings.HISTORY_DB_PATH),
               settings=settings, cache=cache, seller_id=seller_id)


async def _cached(cache: Cache, settings: Settings, key: str, fn) -> Any:  # noqa: ANN001
    hit = await cache.get_json(key)
    if hit is not None:
        return hit
    out = await fn()
    await cache.set_json(key, out, max(seconds_until_sync(settings), settings.KPI_CACHE_TTL_SECONDS))
    return out


@router.get("")
async def ranking(filters: KpiFilters = Depends(parse), refresh: bool = False, user: User = Depends(current_user),
                  odoo: OdooClient = Depends(get_odoo), cache: Cache = Depends(get_cache),
                  settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    require_module(user, "ventas")
    scoped = user.scope_filters(filters)
    ctx = _ctx(odoo, cache, settings, scoped)
    cur, prev = periods(scoped)

    async def compute() -> dict[str, Any]:
        rows, prev_rows = await asyncio.gather(ventas.seller_ranking(ctx, cur),
                                               ventas.seller_ranking(ctx, prev) if prev else asyncio.sleep(0, []))
        before = {r["id"]: r for r in prev_rows or []}
        for r in rows:
            p = before.get(r["id"])
            r["facturado_anterior"] = p["facturado"] if p else None
            r["rank_anterior"] = p["rank"] if p else None
        return {"periodo": {"date_from": cur[0].isoformat(), "date_to": cur[1].isoformat()},
                "anterior": {"date_from": prev[0].isoformat(), "date_to": prev[1].isoformat()} if prev else None,
                "iva": scoped.iva, "total": round(sum(r["facturado"] for r in rows), 2), "vendedores": rows}
    try:
        key = make_key("vendedores", scoped.model_dump(mode="json"))
        return await compute() if refresh else await _cached(cache, settings, key, compute)
    except ScopeRestricted as exc:
        raise HTTPException(409, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc


async def _invoices(ctx: Ctx, cur: tuple[date, date]) -> Table:
    inv = M.CUSTOMER_INVOICE
    dom = await build_domain(ctx, inv, "Facturación", period=cur,
                             extra=[(inv.f("move_type"), "in", ["out_invoice", "out_refund"]), *ventas.seller_move(ctx)])
    amount = inv.f("total") if ventas.with_tax(ctx) else inv.f("untaxed")
    rows = await ctx.odoo.search_read(inv.model, dom, ["id", "name", "invoice_date", "invoice_date_due", "partner_id",
                                                       "branch_office_id", amount, "amount_residual_signed",
                                                       "payment_state", "move_type"],
                                      order=f"{amount} desc", limit=300)
    iva = ventas.tax_label(ctx)
    return Table(
        key="facturas_vendedor", title=f"Sus facturas y notas de crédito del periodo (las 300 mayores, {iva})",
        ai_include=False, odoo_model="account.move",
        columns=[Column(key="factura", label="Documento"), Column(key="fecha", label="Fecha", type="date"),
                 Column(key="cliente", label="Cliente", entity="cliente"), Column(key="sucursal", label="Sucursal"),
                 Column(key="importe", label=f"Importe ({iva})", type="money"),
                 Column(key="saldo", label="Saldo por cobrar (con IVA)", type="money"),
                 Column(key="vence", label="Vence", type="date"), Column(key="estado", label="Estado de pago")],
        rows=[{"_id": r["id"], "factura": r["name"], "fecha": r["invoice_date"], "cliente": m2o_label(r["partner_id"]),
               "sucursal": m2o_label(r.get("branch_office_id")), "importe": round(num(r.get(amount)), 2),
               "saldo": round(num(r.get("amount_residual_signed")), 2), "vence": r.get("invoice_date_due") or None,
               "estado": "Nota de crédito" if r["move_type"] == "out_refund" else PAY_ES.get(r.get("payment_state"), "")}
              for r in rows])


async def _commission(ctx: Ctx, seller_id: int) -> dict[str, Any] | None:
    from ..services.commission_insights import build_panel

    panel = await build_panel(ctx)
    s = next((v for v in panel["vendedores"] if v.get("id") == seller_id), None)
    if not s:
        return None
    keys = ("comision", "base", "meta", "cumplimiento", "pct", "puesto", "siguiente", "potencial_comision",
            "potencial_base", "riesgo_comision", "riesgo_base", "facturas", "observaciones")
    return {**{k: s.get(k) for k in keys}, "preliminar": panel.get("preliminar"), "meses": panel.get("meses")}


@router.get("/{seller_id}")
async def profile(seller_id: int, filters: KpiFilters = Depends(parse), refresh: bool = False,
                  user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                  cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    require_module(user, "ventas")
    scoped = user.scope_filters(filters)
    cur, prev = periods(scoped)

    async def compute() -> dict[str, Any]:
        ctx = _ctx(odoo, cache, settings, scoped, seller_id)
        names = await odoo.search_read("res.users", [("id", "=", seller_id), ("active", "in", [True, False])],
                                       ["name"], limit=1)
        if not names:
            raise HTTPException(404, "Vendedor no encontrado")
        cards, charts, tables = await ventas.build(ctx)
        tables.insert(0, await _invoices(ctx, cur))
        team = _ctx(odoo, cache, settings, scoped)
        ranking_rows, prev_rows = await asyncio.gather(
            ventas.seller_ranking(team, cur), ventas.seller_ranking(team, prev) if prev else asyncio.sleep(0, []))
        mine = next((r for r in ranking_rows if r["id"] == seller_id), None)
        if mine:
            before = next((r for r in prev_rows or [] if r["id"] == seller_id), None)
            mine["facturado_anterior"] = before["facturado"] if before else None
            mine["rank_anterior"] = before["rank"] if before else None
        kpis = ModuleKpis(module="ventas", filters=scoped, period=Period(date_from=cur[0], date_to=cur[1]),
                          previous_period=Period(date_from=prev[0], date_to=prev[1]) if prev else None,
                          cards=cards, charts=charts, tables=tables, notes=ctx.notes,
                          generated_at=datetime.now(timezone.utc))
        return {"id": seller_id, "nombre": names[0]["name"], "ranking": mine, "vendedores": len(ranking_rows),
                "kpis": kpis.model_dump(mode="json")}
    try:
        key = make_key("vendedor", seller_id, scoped.model_dump(mode="json"))
        out = await compute() if refresh else await _cached(cache, settings, key, compute)
    except (ModuleUnavailable, ScopeRestricted) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    # Comisión (sin IVA, por mes calendario): solo para quien ve el módulo de comisiones; usa su propia caché.
    out["comision"] = None
    if user.can("comisiones"):
        try:
            cf = scoped.model_copy(update={"iva": "sin", "warehouse_id": None, "compare": "none"})
            out["comision"] = await _commission(_ctx(odoo, cache, settings, cf), seller_id)
        except (ModuleUnavailable, ScopeRestricted, OdooError, KeyError):
            out["comision"] = None
    return out
