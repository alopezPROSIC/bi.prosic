"""Resumen semanal por correo: KPIs clave de la semana anterior, alertas por umbral y (opcional) análisis de IA.

Los correos van al personal interno de PROSIC (SUMMARY_RECIPIENTS); no se envía nada a terceros.
"""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from ..cache import Cache
from ..config import Settings
from ..odoo_client import OdooClient
from ..schemas.kpis import KpiCard, KpiFilters, ModuleKpis
from .registry import card_index, get_module_kpis

log = logging.getLogger("prosic.summary")

MODULES = [("ventas", "Ventas y facturación"), ("rentas", "Rentas e inventario"),
           ("mantenimiento", "Mantenimiento"), ("compras", "Compras")]
# Tarjetas que aparecen en el correo (en ese orden) por módulo.
HIGHLIGHTS = {
    "ventas": ["ventas", "facturado", "cobrado", "cxc_total", "cxc_vencida_pct", "cxc_dso"],
    "rentas": ["ingresos_renta", "utilizacion_actual", "utilizacion_periodo", "unidades_mantenimiento",
               "unidades_retorno", "equipos_ociosos", "vencen_7"],
    "mantenimiento": ["peticiones", "abiertas", "correctivos_pct", "resolucion_dias", "costo_reparacion"],
    "compras": ["gasto", "pendiente_recibir", "cxp_total", "cxp_vencida"],
}


def last_week(today: date) -> tuple[date, date]:
    """Lunes a domingo de la semana anterior."""
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "MXN":
        return f"${value:,.0f}"
    if unit == "%":
        return f"{value:,.1f}%"
    if unit == "days":
        return f"{value:,.1f} días"
    return f"{value:,.0f}"


def alerts(kpis: dict[str, ModuleKpis], s: Settings) -> list[str]:
    out: list[str] = []
    r = card_index(kpis["rentas"]) if "rentas" in kpis else {}
    v = card_index(kpis["ventas"]) if "ventas" in kpis else {}

    def val(cards: dict[str, KpiCard], key: str) -> float | None:
        c = cards.get(key)
        return c.value if c else None

    util = val(r, "utilizacion_actual")
    if util is not None and util < s.ALERT_UTILIZATION_MIN_PCT:
        out.append(f"Utilización actual de la flota en {util:.1f}% (mínimo esperado {s.ALERT_UTILIZATION_MIN_PCT:.0f}%).")
    fleet, quarantine = val(r, "unidades_total"), val(r, "unidades_mantenimiento")
    if fleet and quarantine is not None and quarantine / fleet * 100 > s.ALERT_QUARANTINE_MAX_PCT:
        out.append(f"{quarantine:.0f} unidades en mantenimiento ({quarantine / fleet * 100:.1f}% de la flota; "
                   f"máximo esperado {s.ALERT_QUARANTINE_MAX_PCT:.0f}%).")
    overdue = val(v, "cxc_vencida_pct")
    if overdue is not None and overdue > s.ALERT_AR_OVERDUE_MAX_PCT:
        out.append(f"CxC vencida en {overdue:.1f}% del saldo (máximo esperado {s.ALERT_AR_OVERDUE_MAX_PCT:.0f}%).")
    for key in ("vencen_7",):
        n = val(r, key)
        if n:
            out.append(f"{n:.0f} contratos de renta vencen en los próximos 7 días: planear retornos e inspección.")
    return out


def render_html(period: tuple[date, date], kpis: dict[str, ModuleKpis], alert_list: list[str],
                ai: dict[str, str], dashboard_url: str | None) -> str:
    e = html.escape
    parts = [
        "<div style=\"font-family:Figtree,Segoe UI,Arial,sans-serif;max-width:680px;color:#154046\">",
        "<div style=\"background:#154046;color:#f4fff3;padding:14px 18px;border-bottom:4px solid #33e989\">"
        "<strong style=\"font-size:18px\">PROSIC BI</strong> · Resumen semanal</div>",
        f"<p style=\"margin:10px 0 16px;color:#5f777a\">Semana del {period[0]:%d/%m/%Y} al {period[1]:%d/%m/%Y} "
        "(comparada con la semana previa)</p>",
    ]
    if alert_list:
        parts.append("<div style=\"border-left:4px solid #b45309;background:#fffbeb;padding:10px 14px;margin-bottom:16px\">"
                     "<strong>Alertas</strong><ul style=\"margin:6px 0 0;padding-left:18px\">"
                     + "".join(f"<li>{e(a)}</li>" for a in alert_list) + "</ul></div>")
    for module, title in MODULES:
        k = kpis.get(module)
        if not k:
            continue
        parts.append(f"<h3 style=\"margin:18px 0 6px\">{e(title)}</h3>")
        if not k.available:
            parts.append(f"<p style=\"color:#6b7280\">No disponible: {e(k.reason or '')}</p>")
            continue
        idx = card_index(k)
        rows = []
        for key in HIGHLIGHTS.get(module, []):
            c = idx.get(key)
            if not c:
                continue
            delta = ""
            if c.delta_pct is not None:
                good = (c.delta_pct >= 0) == c.higher_is_better
                color = "#047857" if good else "#b91c1c"
                delta = f"<span style=\"color:{color}\">{'▲' if c.delta_pct >= 0 else '▼'} {abs(c.delta_pct):.1f}%</span>"
            rows.append(f"<tr><td style=\"padding:4px 8px;border-bottom:1px solid #e5e7eb\">{e(c.label)}</td>"
                        f"<td style=\"padding:4px 8px;border-bottom:1px solid #e5e7eb;text-align:right\"><strong>{fmt(c.value, c.unit)}</strong></td>"
                        f"<td style=\"padding:4px 8px;border-bottom:1px solid #e5e7eb;text-align:right\">{delta}</td></tr>")
        parts.append("<table style=\"border-collapse:collapse;width:100%;font-size:14px\">" + "".join(rows) + "</table>")
        if ai.get(module):
            parts.append(f"<p style=\"background:#f3f4f6;padding:8px 12px;font-size:14px\"><strong>Análisis IA:</strong> "
                         f"{e(ai[module])}</p>")
    if dashboard_url:
        parts.append(f"<p style=\"margin-top:20px\"><a href=\"{e(dashboard_url)}\">Abrir el dashboard</a></p>")
    parts.append("<p style=\"color:#9ca3af;font-size:12px\">Generado automáticamente por PROSIC BI.</p></div>")
    return "".join(parts)


async def build_summary(odoo: OdooClient, cache: Cache, settings: Settings, today: date | None = None,
                        with_ai: bool = True) -> tuple[str, str]:
    """Devuelve (asunto, html)."""
    today = today or datetime.now(ZoneInfo(settings.ODOO_TZ)).date()
    period = last_week(today)
    filters = KpiFilters(date_from=period[0], date_to=period[1], compare="previous")
    kpis: dict[str, ModuleKpis] = {}
    for module, _ in MODULES:
        try:
            kpis[module] = await get_module_kpis(odoo, cache, settings, module, filters)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — un módulo caído no detiene el correo
            log.warning("Resumen: el módulo %s falló: %s", module, exc)
    # La foto de flota (utilización actual, mantenimiento) debe ser la de HOY, no la de la semana pasada.
    try:
        today_rentas = await get_module_kpis(odoo, cache, settings, "rentas",
                                             KpiFilters(date_from=today.replace(day=1), date_to=today, compare="none"))
        if "rentas" in kpis:
            snapshot_keys = {"utilizacion_actual", "unidades_total", "unidades_mantenimiento", "unidades_retorno",
                             "equipos_ociosos", "vencen_7"}
            merged = [c for c in kpis["rentas"].cards if c.key not in snapshot_keys]
            merged += [c for c in today_rentas.cards if c.key in snapshot_keys]
            kpis["rentas"] = kpis["rentas"].model_copy(update={"cards": merged})
    except Exception as exc:  # noqa: BLE001
        log.warning("Resumen: no se pudo obtener la foto actual de flota: %s", exc)

    ai: dict[str, str] = {}
    if with_ai and settings.ai_enabled:
        from ..ai.analyze import run_analysis  # import diferido: evita ciclos

        for module in ("rentas", "ventas"):  # pocos llamados: cuida los límites del plan gratuito de Groq
            try:
                ai[module] = (await run_analysis(odoo, cache, settings, "resumen-semanal", module, filters)).resumen  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                log.warning("Resumen: análisis IA de %s falló: %s", module, exc)
    alert_list = alerts(kpis, settings)
    dashboard = settings.cors_origins[0] if settings.cors_origins else None
    subject = f"PROSIC BI · Resumen semanal {period[0]:%d/%m} – {period[1]:%d/%m}"
    if alert_list:
        subject += f" · {len(alert_list)} alerta(s)"
    return subject, render_html(period, kpis, alert_list, ai, dashboard)


def recipients(settings: Settings) -> list[str]:
    return [r.strip() for r in settings.SUMMARY_RECIPIENTS.split(",") if r.strip()]


def email_enabled(settings: Settings) -> bool:
    return bool(settings.SMTP_HOST and recipients(settings))


def _send(settings: Settings, subject: str, body_html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = ", ".join(recipients(settings))
    msg.set_content("Este correo requiere un cliente compatible con HTML.")
    msg.add_alternative(body_html, subtype="html")
    if settings.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=ssl.create_default_context(), timeout=30) as s:
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
        if settings.SMTP_TLS:
            s.starttls(context=ssl.create_default_context())
        if settings.SMTP_USER:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        s.send_message(msg)


async def send_summary(odoo: OdooClient, cache: Cache, settings: Settings) -> dict[str, Any]:
    if not email_enabled(settings):
        raise RuntimeError("Configura SMTP_HOST y SUMMARY_RECIPIENTS para enviar el resumen")
    subject, body = await build_summary(odoo, cache, settings)
    await asyncio.to_thread(_send, settings, subject, body)
    log.info("Resumen semanal enviado a %d destinatario(s)", len(recipients(settings)))
    return {"sent_to": recipients(settings), "subject": subject}
