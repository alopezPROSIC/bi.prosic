"""Panel de administración: usuarios, roles, configuración general, bitácora de accesos y resumen semanal."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import User, require_admin
from ..cache import Cache
from ..config import Settings, get_settings, runtime_settings
from ..deps import get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError
from ..services.summary import build_summary, email_enabled, recipients, send_summary
from ..store import EDITABLE_SETTINGS, MODULES, STATUSES, Role, UserRec, get_store, role_dict

router = APIRouter(prefix="/api/admin", tags=["admin"])
_ROLE_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def _store():
    return get_store(get_settings().HISTORY_DB_PATH)


def _bad(exc: ValueError) -> HTTPException:
    return HTTPException(400, str(exc))


# ------------------------------------------------------------------ usuarios
class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=200)
    role: str | None = None
    status: str = "activo"
    branch_ids: list[int] = []
    warehouse_ids: list[int] = []


@router.get("/users")
async def list_users(_: User = Depends(require_admin)) -> dict[str, Any]:
    s = get_settings()
    return {"users": [u.__dict__ for u in _store().users()], "statuses": STATUSES,
            "admin_emails": sorted(s.admin_emails), "domain": s.ALLOWED_EMAIL_DOMAIN}


@router.put("/users")
async def save_user(body: UserIn, request: Request, admin: User = Depends(require_admin)) -> dict[str, Any]:
    s = get_settings()
    email = body.email.strip().lower()
    if not email.endswith("@" + s.ALLOWED_EMAIL_DOMAIN.lower()):
        raise HTTPException(400, f"Solo se permiten correos @{s.ALLOWED_EMAIL_DOMAIN}")
    store = _store()
    current = store.user(email)
    try:
        saved = store.upsert_user(UserRec(email=email, name=body.name, role=body.role, status=body.status,
                                          branch_ids=body.branch_ids, warehouse_ids=body.warehouse_ids,
                                          created_at=current.created_at if current else "",
                                          last_login=current.last_login if current else None))
    except ValueError as exc:
        raise _bad(exc) from exc
    store.log(email, "usuario_actualizado" if current else "usuario_creado", None,
              f"por {admin.username}: rol={body.role} estado={body.status} sucursales={body.branch_ids} "
              f"almacenes={body.warehouse_ids}")
    return saved.__dict__


@router.delete("/users/{email}")
async def delete_user(email: str, admin: User = Depends(require_admin)) -> dict[str, bool]:
    try:
        _store().delete_user(email)
    except ValueError as exc:
        raise _bad(exc) from exc
    _store().log(email, "usuario_eliminado", None, f"por {admin.username}")
    return {"ok": True}


# ------------------------------------------------------------------ roles
class RoleIn(BaseModel):
    name: str
    label: str = Field(min_length=1, max_length=60)
    modules: list[str] = []
    ai: bool = False
    admin: bool = False


@router.get("/roles")
async def list_roles(_: User = Depends(require_admin)) -> dict[str, Any]:
    return {"roles": [role_dict(r) for r in _store().roles()], "modules": MODULES}


@router.put("/roles")
async def save_role(body: RoleIn, admin: User = Depends(require_admin)) -> dict[str, Any]:
    if not _ROLE_RE.match(body.name):
        raise HTTPException(400, "Nombre de rol inválido (minúsculas, números y _; 2 a 32 caracteres)")
    unknown = set(body.modules) - set(MODULES)
    if unknown:
        raise HTTPException(400, f"Módulos desconocidos: {', '.join(sorted(unknown))}")
    _store().upsert_role(Role(body.name, body.label, body.modules, body.ai, body.admin))
    _store().log(admin.username, "rol_actualizado", None, f"{body.name}: módulos={body.modules} ia={body.ai} admin={body.admin}")
    return role_dict(_store().role(body.name))  # type: ignore[arg-type]


@router.delete("/roles/{name}")
async def delete_role(name: str, admin: User = Depends(require_admin)) -> dict[str, bool]:
    try:
        _store().delete_role(name)
    except ValueError as exc:
        raise _bad(exc) from exc
    _store().log(admin.username, "rol_eliminado", None, name)
    return {"ok": True}


# ------------------------------------------------------------------ configuración general
def _settings_view() -> dict[str, Any]:
    base, eff = get_settings(), runtime_settings()
    overrides = _store().settings()
    return {
        "values": {k: getattr(eff, k) for k in EDITABLE_SETTINGS},
        "overridden": sorted(overrides),
        "env_defaults": {k: getattr(base, k) for k in EDITABLE_SETTINGS},
        # Solo lectura (se cambian en .env): nunca se exponen claves ni contraseñas.
        "info": {
            "odoo_url": base.ODOO_URL, "odoo_db": base.ODOO_DB, "odoo_user": base.ODOO_USER,
            "ai_provider": base.AI_PROVIDER, "ai_model": base.ai_model, "ai_key_configured": bool(base.ai_api_key),
            "google_login": bool(base.GOOGLE_CLIENT_ID), "allowed_domain": base.ALLOWED_EMAIL_DOMAIN,
            "password_login": base.password_login, "smtp_configured": bool(base.SMTP_HOST),
            "prefetch_minutes": base.PREFETCH_MINUTES,
        },
    }


@router.get("/settings")
async def get_settings_view(_: User = Depends(require_admin)) -> dict[str, Any]:
    return _settings_view()


@router.put("/settings")
async def update_settings(values: dict[str, Any], admin: User = Depends(require_admin),
                          cache: Cache = Depends(get_cache)) -> dict[str, Any]:
    try:
        _store().update_settings(values)
    except ValueError as exc:
        raise _bad(exc) from exc
    _store().log(admin.username, "configuracion", None, ", ".join(f"{k}={v}" for k, v in values.items()))
    if "KPI_CACHE_TTL_SECONDS" in values or "AI_ANONYMIZE" in values:
        await cache.delete_prefix("kpi")
    return _settings_view()


# ------------------------------------------------------------------ bitácora
@router.get("/access-log")
async def access_log(limit: int = Query(300, ge=1, le=2000), email: str | None = None,
                     _: User = Depends(require_admin)) -> dict[str, Any]:
    return {"events": _store().access_log(limit, email)}


# ------------------------------------------------------------------ resumen semanal
@router.get("/summary/status")
async def summary_status(_: User = Depends(require_admin), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    return {"email_enabled": email_enabled(settings), "recipients": recipients(settings),
            "weekday": settings.SUMMARY_WEEKDAY, "hour": settings.SUMMARY_HOUR}


@router.get("/summary/preview", response_class=HTMLResponse)
async def summary_preview(ai: bool = False, _: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                          cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> HTMLResponse:
    """Vista previa del correo semanal (no envía nada). ``ai=true`` incluye el análisis de IA."""
    try:
        subject, body = await build_summary(odoo, cache, settings, with_ai=ai)
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    return HTMLResponse(f"<title>{subject}</title>{body}")


@router.post("/summary/send")
async def summary_send(_: User = Depends(require_admin), odoo: OdooClient = Depends(get_odoo),
                       cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> dict[str, Any]:
    """Envía el resumen ahora (prueba de configuración SMTP)."""
    try:
        return await send_summary(odoo, cache, settings)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:  # smtplib / red
        raise HTTPException(502, f"No se pudo enviar el correo: {exc}") from exc
