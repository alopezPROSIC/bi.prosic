"""Comisiones — captura del plan (Fase 02), datos del mes (metas, cálculo manual, ajustes) y estados de cuenta (Fase 05).

Administradores. Las METAS del mes también las captura quien tenga el permiso «metas» (Director Comercial,
gerentes): solo metas, y un usuario limitado a sucursales solo las de sus sucursales. Todo se guarda en la base
local del BI; nada se escribe en Odoo.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import HTMLResponse, Response

from ..auth import User, current_user, require_admin, require_module
from ..cache import Cache
from ..config import Settings, get_settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError
from .. import odoo_mapping as M
from ..schemas.kpis import KpiFilters
from ..services.commission_plan import (MONTH_RE, MesDatos, Plan, get_plan_store, month_label, month_range,
                                        months_between, plan_to_json)
from ..services.commission_report import build_xlsx, month_kpis, recipients_for, render_statement, send_statements
from ..services.registry import get_module_kpis
from ..services.common import m2o_id, m2o_label
from ..store import get_store

router = APIRouter(prefix="/api/comisiones", tags=["comisiones"])
MonthPath = Path(..., pattern=MONTH_RE.pattern)


def _plans():
    return get_plan_store(get_settings().HISTORY_DB_PATH)


def _log(user: User, event: str, detail: str) -> None:
    get_store(get_settings().HISTORY_DB_PATH).log(user.username, event, None, detail)


def require_metas(user: User = Depends(current_user)) -> User:
    if not (user.is_admin or user.can("metas")):
        raise HTTPException(403, "Tu rol no tiene permiso para capturar metas de comisiones")
    return user


def _metas_only(d: MesDatos) -> dict[str, Any]:
    return {"metas_vendedor": d.metas_vendedor, "metas_sucursal": d.metas_sucursal, "manual": {}, "ajustes": [],
            "ratio_vendedor": {}, "ratio_sucursal": {}}


@router.get("/plan")
async def get_plan(user: User = Depends(require_metas)) -> dict[str, Any]:
    plan = plan_to_json(_plans().plan())
    if not user.is_admin:  # captura de metas: solo lo necesario (puestos, vendedores y sucursales)
        plan["depuracion"] = {**plan["depuracion"], "reglas": []}
    return {"plan": plan, **_plans().plan_info()}


@router.put("/plan")
async def save_plan(plan: Plan, admin: User = Depends(require_admin)) -> dict[str, Any]:
    _plans().save_plan(plan, admin.username)
    _log(admin, "comisiones_plan", f"{len(plan.puestos)} puestos, {len(plan.vendedores)} vendedores, "
                                   f"{len(plan.sucursales)} sucursales, ocupación={'sí' if plan.ocupacion.activo else 'no'}, "
                                   f"cuentas={','.join(plan.depuracion.cuentas) or 'todas'}, {len(plan.depuracion.reglas)} reglas")
    return {"plan": plan_to_json(_plans().plan()), **_plans().plan_info()}


@router.get("/vendedores-odoo")
async def odoo_sellers(_: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                       cache: Cache = Depends(get_cache)) -> dict[str, Any]:
    """Vendedores que facturaron en los últimos 18 meses (para darlos de alta en el plan). Una consulta, en caché 1 día."""
    key = "prosic:comis-vendedores"
    hit = await cache.get_json(key)
    if hit:
        return hit
    since = (date.today() - timedelta(days=548)).isoformat()
    try:
        groups = await odoo.read_group(
            "account.move", [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("invoice_date", ">=", since)],
            ["invoice_user_id"], ["invoice_user_id"])
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    out = {"vendedores": sorted(
        ({"odoo_id": m2o_id(g["invoice_user_id"]), "nombre": m2o_label(g["invoice_user_id"]),
          "facturas": g.get("__count", 0)} for g in groups if g.get("invoice_user_id")),
        key=lambda x: x["nombre"])}
    await cache.set_json(key, out, 86400)
    return out


@router.get("/cuentas")
async def accounts(_: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                   cache: Cache = Depends(get_cache)) -> dict[str, Any]:
    """Cuentas de ingreso de la lista de Finanzas (para elegir cuáles comisionan). Una consulta, en caché 1 día."""
    key = "prosic:comis-cuentas"
    hit = await cache.get_json(key)
    if hit:
        return hit
    from fnmatch import fnmatch

    patterns = M.COMMISSION_INCOME_ACCOUNTS + M.COMMISSION_CREDIT_NOTE_ACCOUNTS
    names: dict[str, str] = {}
    try:
        for a in await odoo.search_read("account.account", [], ["code", "name"], limit=20000):
            if any(fnmatch(a.get("code") or "", p.replace("%", "*")) for p in patterns):
                names[a["code"]] = a.get("name") or ""
    except OdooError:
        pass  # sin Odoo se muestran solo los códigos

    def is_nc(code: str) -> bool:
        return any(fnmatch(code, p.replace("%", "*")) for p in M.COMMISSION_CREDIT_NOTE_ACCOUNTS)
    # Un patrón (p. ej. 401-001-% = todas las rentas) se ofrece como opción propia, seguido de sus cuentas.
    rows: list[dict[str, Any]] = []
    for p in patterns:
        if "%" in p:
            rows.append({"code": p, "nombre": "Todas las rentas" if p == "401-001-%" else f"Todas las {p}",
                         "nota_credito": is_nc(p)})
            rows += [{"code": c, "nombre": n, "nota_credito": is_nc(c)} for c, n in sorted(names.items())
                     if fnmatch(c, p.replace("%", "*"))]
        else:
            rows.append({"code": p, "nombre": names.get(p, ""), "nota_credito": is_nc(p)})
    out = {"cuentas": rows}
    if names:
        await cache.set_json(key, out, 86400)
    return out


@router.get("/depurado.xlsx")
async def depurated_excel(desde: str = Query(..., pattern=MONTH_RE.pattern), hasta: str = Query(..., pattern=MONTH_RE.pattern),
                          admin: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                          cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> Response:
    """Excel depurado (resumen por vendedor, facturas con su vendedor final y regla aplicada, reglas vigentes)."""
    if hasta < desde:
        raise HTTPException(400, "El mes final es anterior al inicial")
    if len(months_between(month_range(desde)[0], month_range(hasta)[0])) > 12:
        raise HTTPException(400, "El Excel abarca como máximo 12 meses")
    if desde == hasta:
        k = await _kpis_or_502(month_kpis(odoo, cache, settings, desde))
    else:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
        d0, d1 = month_range(desde)[0], month_range(hasta)[1]
        k = await _kpis_or_502(get_module_kpis(odoo, cache, settings, "comisiones",
                                               KpiFilters(date_from=d0, date_to=min(d1, max(today, d0)), compare="none")))
    if not k.available:
        raise HTTPException(400, k.reason or "Comisiones no disponible")
    title = month_label(desde) if desde == hasta else f"{month_label(desde)} a {month_label(hasta)}"
    data = build_xlsx(k, settings, title)
    _log(admin, "comisiones_excel", f"Excel depurado {desde}..{hasta}")
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="comisiones_depurado_{desde}_{hasta}.xlsx"'})


async def _kpis_or_502(coro):
    try:
        return await coro
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc


@router.get("/mes/{month}")
async def get_month(month: str = MonthPath, user: User = Depends(require_metas)) -> dict[str, Any]:
    d = _plans().month(month)
    datos = d.model_dump(mode="json") if user.is_admin else MesDatos(**_metas_only(d)).model_dump(mode="json")
    return {"mes": month, "datos": datos, **_plans().month_info(month)}


def _merge_metas(user: User, month: str, data: MesDatos) -> MesDatos:
    """Captura de metas (no admin): solo cambian las metas y, si el usuario está limitado a sucursales, solo las de
    vendedores / sucursales de su alcance. Cálculo manual, ajustes y ratios se conservan como estaban."""
    cur = _plans().month(month)
    branches = set(user.branch_ids or ())
    if not branches:
        return cur.model_copy(update={"metas_vendedor": data.metas_vendedor, "metas_sucursal": data.metas_sucursal})
    plan = _plans().plan()
    mine = {v.odoo_id for v in plan.vendedores if v.sucursal_id in branches}
    mv = {k: v for k, v in cur.metas_vendedor.items() if k not in mine}
    mv.update({k: v for k, v in data.metas_vendedor.items() if k in mine})
    ms = {k: v for k, v in cur.metas_sucursal.items() if k not in branches}
    ms.update({k: v for k, v in data.metas_sucursal.items() if k in branches})
    return cur.model_copy(update={"metas_vendedor": mv, "metas_sucursal": ms})


@router.put("/mes/{month}")
async def save_month(data: MesDatos, month: str = MonthPath, admin: User = Depends(require_metas)) -> dict[str, Any]:
    if not admin.is_admin:
        data = _merge_metas(admin, month, data)
    try:
        _plans().save_month(month, data, admin.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _log(admin, "comisiones_mes", f"{month}: {len(data.metas_vendedor)} metas de vendedor, {len(data.metas_sucursal)} "
                                  f"de sucursal, {len(data.manual)} cálculos manuales, {len(data.ajustes)} ajustes")
    saved = _plans().month(month)
    datos = saved.model_dump(mode="json") if admin.is_admin else MesDatos(**_metas_only(saved)).model_dump(mode="json")
    return {"mes": month, "datos": datos, **_plans().month_info(month)}


@router.get("/puestos-dh")
async def hr_positions(_: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo)) -> dict[str, Any]:
    """Puesto y sucursal de cada vendedor según su ficha de DH en Odoo (hr.employee). Lo aplica la pantalla."""
    try:
        emps = await odoo.search_read("hr.employee", [("job_id.name", "in", list(M.COMMISSION_HR_JOBS))],
                                      ["name", "job_id", "user_id", "branch_office_id"], limit=2000)
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    rows = [{"odoo_id": m2o_id(e.get("user_id")), "nombre": m2o_label(e.get("user_id")) if e.get("user_id") else e["name"],
             "empleado": e["name"], "puesto_dh": m2o_label(e.get("job_id")), "puesto": M.COMMISSION_HR_JOBS.get(m2o_label(e.get("job_id"))),
             "sucursal_id": m2o_id(e.get("branch_office_id")), "sucursal": m2o_label(e.get("branch_office_id"))}
            for e in emps]
    return {"empleados": sorted(rows, key=lambda r: (r["sucursal"] or "", r["puesto"] or "", r["nombre"])),
            "puestos": M.COMMISSION_HR_JOBS}


@router.get("/envios")
async def sending_status(mes: str = Query(..., pattern=MONTH_RE.pattern), _: User = Depends(require_admin),
                         settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    return {"smtp": bool(settings.SMTP_HOST), "automatico": settings.COMMISSION_REPORTS_ENABLED,
            "dia": settings.COMMISSION_REPORT_DAY, "copia": settings.COMMISSION_REPORT_CC,
            "ultimo_envio_automatico": _plans().get("reports:last_month"), "destinatarios": recipients_for(settings)}


@router.get("/estado-cuenta", response_class=HTMLResponse)
async def statement(mes: str = Query(..., pattern=MONTH_RE.pattern), vendedor_id: int = Query(..., ge=1),
                    _: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                    cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> HTMLResponse:
    """Vista previa del estado de cuenta (no envía nada)."""
    try:
        k = await month_kpis(odoo, cache, settings, mes)
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    if not k.available:
        raise HTTPException(400, k.reason or "Comisiones no disponible")
    st = render_statement(k, mes, vendedor_id, settings)
    if not st:
        raise HTTPException(404, "Ese vendedor no está en el plan o no tiene cálculo en ese mes")
    return HTMLResponse(f"<title>{st[0]}</title>{st[1]}")


@router.post("/enviar")
async def send(mes: str = Query(..., pattern=MONTH_RE.pattern), vendedor_id: int | None = Query(None, ge=1),
               admin: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
               cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    try:
        out = await send_statements(odoo, cache, settings, mes, only=vendedor_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    _log(admin, "comisiones_envio", f"{mes}: {len(out['enviados'])} enviados, {len(out['omitidos'])} omitidos")
    return out


# ------------------------------------------------------------------ panel inteligente (quien ve el módulo)
@router.get("/panel")
async def commission_panel(date_from: date = Query(...), date_to: date = Query(...),
                           company_id: int | None = Query(None, ge=1), branch_id: int | None = Query(None, ge=1),
                           warehouse_id: int | None = Query(None, ge=1), compare: str = Query("previous"),
                           user: User = Depends(current_user), odoo: OdooClient = Depends(get_odoo),
                           cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    """De lo cobrado a la comisión, qué revisar, comisión potencial de la cartera y ficha por vendedor.

    Usa la misma Fase 01 en caché que el módulo (mismos filtros = misma llave) y el plan local."""
    from pydantic import ValidationError

    from ..services.commission_insights import build_panel
    from ..services.common import Ctx, ModuleUnavailable, ScopeRestricted
    from ..services.history import get_history

    require_module(user, "comisiones")
    try:
        filters = KpiFilters(date_from=date_from, date_to=date_to, company_id=company_id, branch_id=branch_id,
                             warehouse_id=warehouse_id, compare=compare)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    scoped = user.scope_filters(filters)
    ctx = Ctx(odoo=odoo, filters=scoped, tz=settings.ODOO_TZ, history=get_history(settings.HISTORY_DB_PATH),
              settings=settings, cache=cache)
    try:
        return await build_panel(ctx)
    except (ModuleUnavailable, ScopeRestricted) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
