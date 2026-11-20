"""Comisiones — Fase 05: estado de cuenta mensual individual por vendedor y por gerente de sucursal.

- Vendedor: su renglón del cálculo (base, meta, cumplimiento, %, factor, ajustes, comisión) y sus facturas cobradas.
- Gerente (puesto que comisiona por sucursal): además, el resumen de todo su equipo de la sucursal.
Solo va al correo interno de cada persona (capturado en el plan) y, opcionalmente, en copia a Finanzas.
El envío automático está apagado (COMMISSION_REPORTS_ENABLED) hasta que Finanzas valide el cálculo (Fase 04).
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..cache import Cache
from ..config import Settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiFilters, ModuleKpis
from .commission_plan import get_plan_store, month_label, month_range
from .registry import get_module_kpis
from .summary import _send

log = logging.getLogger("prosic.comisiones")


def previous_month(today: date) -> str:
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{y:04d}-{m:02d}"


async def month_kpis(odoo: OdooClient, cache: Cache, settings: Settings, month: str) -> ModuleKpis:
    d0, d1 = month_range(month)
    today = datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
    return await get_module_kpis(odoo, cache, settings, "comisiones",
                                 KpiFilters(date_from=d0, date_to=min(d1, max(today, d0)), compare="none"))


def _table(k: ModuleKpis, key: str) -> list[dict[str, Any]]:
    return next((t.rows for t in k.tables if t.key == key), [])


def _money(v: Any) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _pct(v: Any) -> str:
    return "—" if v is None else f"{v:,.1f}%"


def recipients_for(settings: Settings) -> list[dict[str, Any]]:
    """Vendedores activos del plan con correo: a quién se le envía el estado de cuenta."""
    plan = get_plan_store(settings.HISTORY_DB_PATH).plan()
    return [{"odoo_id": v.odoo_id, "nombre": v.nombre, "correo": v.correo,
             "gerente": bool((p := plan.puesto(v.puesto)) and p.base == "sucursal")}
            for v in plan.vendedores if v.activo and v.correo]


def render_statement(k: ModuleKpis, month: str, odoo_id: int, settings: Settings) -> tuple[str, str] | None:
    """(asunto, html) del estado de cuenta de un vendedor; None si no tiene renglón ese mes."""
    plan = get_plan_store(settings.HISTORY_DB_PATH).plan()
    v = plan.vendedor(odoo_id)
    calc = [r for r in _table(k, "calculo_comisiones") if r.get("mes") == month]
    me = next((r for r in calc if r.get("vendedor_id") == odoo_id), None)
    if not v or not me:
        return None
    p = plan.puesto(v.puesto)
    gerente = bool(p and p.base == "sucursal")
    e = html.escape
    inv = [r for r in _table(k, "facturas_depuradas") if r.get("estado") == "Comisiona"
           and (r.get("fecha_cobro") or "")[:7] == month and (
        r.get("sucursal_id") == me["sucursal_id"] if gerente else r.get("vendedor_id") == odoo_id)]
    td = "padding:5px 8px;border-bottom:1px solid #e5e7eb"
    tdr = td + ";text-align:right"
    lines = [("Base cobrada" + (" de la sucursal" if gerente else ""), _money(me["base"])),
             ("Meta del mes", _money(me["meta"])), ("Cumplimiento", _pct(me["cumplimiento"])),
             ("% de comisión (tabulador)", _pct(me["pct_comision"])), ("Comisión bruta", _money(me["comision_bruta"]))]
    if me.get("factor") is not None:
        lines.append((f"Factor por ocupación ({_pct(me['ocupacion'])})", f"× {me['factor']:g}"))
    if me.get("ajustes"):
        lines.append(("Ajustes", _money(me["ajustes"])))
    parts = [
        "<div style=\"font-family:Figtree,Segoe UI,Arial,sans-serif;max-width:720px;color:#154046\">",
        "<div style=\"background:#154046;color:#f4fff3;padding:14px 18px;border-bottom:4px solid #33e989\">"
        f"<strong style=\"font-size:18px\">PROSIC BI</strong> · Estado de cuenta de comisiones · {e(month_label(month))}</div>",
        f"<p style=\"margin:12px 0\"><strong>{e(v.nombre)}</strong> · {e(p.label if p else '')} · {e(me['sucursal'])}</p>",
        "<table style=\"border-collapse:collapse;width:100%;font-size:14px\">",
        *[f"<tr><td style=\"{td}\">{e(a)}</td><td style=\"{tdr}\">{e(b)}</td></tr>" for a, b in lines],
        f"<tr><td style=\"{td};font-size:16px\"><strong>Comisión del mes</strong></td>"
        f"<td style=\"{tdr};font-size:16px\"><strong>{_money(me['comision'])}</strong></td></tr></table>",
    ]
    if me.get("observaciones"):
        parts.append(f"<p style=\"border-left:4px solid #b45309;background:#fffbeb;padding:8px 12px;font-size:13px\">"
                     f"{e(me['observaciones'])}</p>")
    if gerente:
        team = [r for r in calc if r.get("sucursal_id") == me["sucursal_id"] and r.get("vendedor_id") != odoo_id]
        if team:
            parts.append("<h3 style=\"margin:20px 0 6px\">Tu equipo en la sucursal</h3>"
                         "<table style=\"border-collapse:collapse;width:100%;font-size:13px\"><tr>"
                         + "".join(f"<th style=\"{td};text-align:left\">{h}</th>" for h in
                                   ("Vendedor", "Base", "Meta", "Cumplimiento", "Comisión")) + "</tr>"
                         + "".join(f"<tr><td style=\"{td}\">{e(r['vendedor'])}</td><td style=\"{tdr}\">{_money(r['base'])}</td>"
                                   f"<td style=\"{tdr}\">{_money(r['meta'])}</td><td style=\"{tdr}\">{_pct(r['cumplimiento'])}</td>"
                                   f"<td style=\"{tdr}\">{_money(r['comision'])}</td></tr>" for r in team) + "</table>")
    parts.append(f"<h3 style=\"margin:20px 0 6px\">Facturas cobradas al 100 % en el mes ({len(inv)})</h3>"
                 "<table style=\"border-collapse:collapse;width:100%;font-size:12px\"><tr>"
                 + "".join(f"<th style=\"{td};text-align:left\">{h}</th>" for h in
                           ("Factura", "Cliente", *(("Vendedor",) if gerente else ()), "Cobrada", "Base")) + "</tr>"
                 + "".join(f"<tr><td style=\"{td}\">{e(r['factura'])}</td><td style=\"{td}\">{e(r.get('cliente') or '')}</td>"
                           + (f"<td style=\"{td}\">{e(r.get('vendedor') or '')}</td>" if gerente else "")
                           + f"<td style=\"{td}\">{e(r['fecha_cobro'] or '')}</td><td style=\"{tdr}\">{_money(r['base_comisionable'])}</td></tr>"
                           for r in sorted(inv, key=lambda r: r["fecha_cobro"] or "")) + "</table>")
    dep = plan.depuracion
    cuentas = ", ".join(p.cuentas if p and p.cuentas else dep.cuentas) or "las cuentas de comisión de Finanzas"
    parts.append("<p style=\"color:#6b7280;font-size:12px;margin-top:18px\">Base = ingresos (sin IVA) de "
                 f"{e(cuentas)} de facturas cobradas al 100 % en el mes con dinero o anticipo, menos sus notas de "
                 f"crédito; no cuentan facturas con más de {dep.max_dias} días al cobro. Incluye los traspasos y ajustes "
                 "autorizados en las reglas de comisiones. Dudas: Finanzas – Cuentas por Cobrar. "
                 "Generado automáticamente por PROSIC BI.</p></div>")
    return f"PROSIC · Comisiones {month_label(month)} · {v.nombre}", "".join(parts)


async def send_statements(odoo: OdooClient, cache: Cache, settings: Settings, month: str,
                          only: int | None = None) -> dict[str, Any]:
    if not settings.SMTP_HOST:
        raise RuntimeError("Configura SMTP_HOST para enviar correos")
    k = await month_kpis(odoo, cache, settings, month)
    if not k.available:
        raise RuntimeError(k.reason or "Comisiones no disponible")
    cc = [x.strip() for x in settings.COMMISSION_REPORT_CC.split(",") if x.strip()]
    sent, skipped = [], []
    for r in recipients_for(settings):
        if only and r["odoo_id"] != only:
            continue
        st = render_statement(k, month, r["odoo_id"], settings)
        if not st:
            skipped.append({"nombre": r["nombre"], "motivo": "sin cálculo ese mes"})
            continue
        try:
            await asyncio.to_thread(_send, settings, st[0], st[1], [r["correo"], *cc])
            sent.append({"nombre": r["nombre"], "correo": r["correo"]})
        except OSError as exc:
            skipped.append({"nombre": r["nombre"], "motivo": f"error SMTP: {exc}"})
    log.info("Estados de cuenta de comisiones %s: %d enviados, %d omitidos", month, len(sent), len(skipped))
    return {"mes": month, "enviados": sent, "omitidos": skipped}


async def maybe_send_monthly(odoo: OdooClient, cache: Cache, settings: Settings) -> None:
    if not (settings.COMMISSION_REPORTS_ENABLED and settings.SMTP_HOST):
        return
    now = datetime.now(ZoneInfo(settings.ODOO_TZ))
    if now.day < settings.COMMISSION_REPORT_DAY or now.hour < settings.SUMMARY_HOUR:
        return
    month = previous_month(now.date())
    store = get_plan_store(settings.HISTORY_DB_PATH)
    if store.get("reports:last_month") == month:
        return
    store.set("reports:last_month", month)  # se marca antes: un SMTP caído no provoca reenvíos en bucle
    try:
        await send_statements(odoo, cache, settings, month)
    except Exception as exc:  # noqa: BLE001
        log.error("No se pudieron enviar los estados de cuenta de comisiones: %s", exc)


# ------------------------------------------------------------------ Excel depurado
def build_xlsx(k: ModuleKpis, settings: Settings, title: str) -> bytes:
    """Libro de Excel con el resultado de la depuración: resumen por vendedor, facturas depuradas, efecto de las
    reglas y la configuración vigente (para auditoría)."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    plan = get_plan_store(settings.HISTORY_DB_PATH).plan()
    dep = plan.depuracion
    names = {v.odoo_id: v.nombre for v in plan.vendedores}
    wb = Workbook()
    head_fill, head_font = PatternFill("solid", fgColor="154046"), Font(bold=True, color="FFFFFF")
    fmt = {"money": "#,##0.00", "number": "#,##0", "percent": "0.0", "days": "#,##0"}

    def sheet(name: str, columns: list[tuple[str, str, str]], rows: list[dict[str, Any]], first: bool = False) -> None:
        ws = wb.active if first else wb.create_sheet()
        ws.title = name[:31]
        ws.append([c[1] for c in columns])
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for r in rows:
            ws.append([r.get(c[0]) for c in columns])
        for i, (_, label, typ) in enumerate(columns, start=1):
            col = get_column_letter(i)
            if typ in fmt:
                for cell in ws[col][1:]:
                    cell.number_format = fmt[typ]
            ws.column_dimensions[col].width = min(48, max(10, len(label) + 2,
                                                          *(len(str(r.get(columns[i - 1][0]) or "")) + 2 for r in rows[:300])))
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions

    def cols(key: str) -> list[tuple[str, str, str]]:
        t = next((t for t in k.tables if t.key == key), None)
        return [(c.key, c.label, c.type) for c in t.columns] if t else []

    sheet("Resumen por vendedor", cols("calculo_comisiones"), _table(k, "calculo_comisiones"), first=True)
    sheet("Facturas depuradas", cols("facturas_depuradas"), _table(k, "facturas_depuradas"))
    sheet("Efecto de reglas", cols("efecto_reglas"), _table(k, "efecto_reglas"))
    rules = [{"activo": "Sí" if r.activo else "No", "accion": r.accion.capitalize(), "motivo": r.motivo,
              "vendedor": names.get(r.vendedor_id or 0, r.vendedor_id or ""), "sucursal": r.sucursal_id or "",
              "cliente": r.cliente, "centro_costo": r.centro_costo, "facturas": ", ".join(r.facturas),
              "desde": r.desde.isoformat() if r.desde else "", "hasta": r.hasta.isoformat() if r.hasta else "",
              "destino": names.get(r.destino_vendedor_id or 0, r.destino_vendedor_id or ""),
              "destino_sucursal": r.destino_sucursal_id or "", "porcentaje": r.porcentaje if r.accion == "reasignar" else ""}
             for r in dep.reglas]
    sheet("Reglas", [("activo", "Activa", "text"), ("accion", "Acción", "text"), ("motivo", "Motivo", "text"),
                     ("vendedor", "Vendedor en Odoo", "text"), ("sucursal", "Sucursal (id)", "text"),
                     ("cliente", "Cliente contiene", "text"), ("centro_costo", "Centro de costo contiene", "text"),
                     ("facturas", "Facturas", "text"), ("desde", "Cobro desde", "text"), ("hasta", "Cobro hasta", "text"),
                     ("destino", "Traspasa a", "text"), ("destino_sucursal", "Sucursal destino (id)", "text"),
                     ("porcentaje", "% traspasado", "number")], rules)
    params = [{"p": "Periodo", "v": title},
              {"p": "Cuentas que comisionan", "v": ", ".join(dep.cuentas) or "Todas las cuentas de comisión de Finanzas"},
              *({"p": f"Cuentas del puesto «{p.label}»", "v": ", ".join(p.cuentas)} for p in plan.puestos if p.cuentas),
              {"p": "Notas de crédito en otras cuentas", "v": {"prorrateo": "Se descuenta la parte proporcional",
                                                               "todas": "Se descuentan completas",
                                                               "ninguna": "No se descuentan"}[dep.notas_credito]},
              {"p": "Días máximos entre emisión y cobro", "v": dep.max_dias},
              {"p": "Generado", "v": k.generated_at.astimezone(ZoneInfo(settings.ODOO_TZ)).strftime("%Y-%m-%d %H:%M")}]
    sheet("Parámetros", [("p", "Parámetro", "text"), ("v", "Valor", "text")], params)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
