from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth import User, authenticate, create_token, current_user, google_user
from ..cache import Cache
from ..config import Settings, get_settings, runtime_settings
from ..deps import get_cache
from ..google_auth import GoogleAuthError, verify_google_credential
from ..store import UserRec, get_store

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class GoogleIn(BaseModel):
    credential: str = Field(min_length=10, max_length=5000)


class UserOut(BaseModel):
    username: str
    name: str = ""
    role: str
    role_label: str = ""
    is_admin: bool = False
    modules: list[str] = []
    ai: bool = False
    warehouse_ids: list[int] = []
    branch_ids: list[int] = []
    # compatibilidad
    warehouse_id: int | None = None
    branch_id: int | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class AuthConfig(BaseModel):
    google_client_id: str | None
    allowed_domain: str
    password_login: bool


def _ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


async def _rate_limit(request: Request, cache: Cache) -> None:
    attempts = await cache.incr_window(f"prosic:rl:login:{_ip(request)}:{datetime.now(timezone.utc):%Y%m%d%H%M}", 70)
    if attempts > 10:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espera un minuto.")


def _out(user: User, settings: Settings) -> UserOut:
    ai = user.ai and settings.ai_enabled
    return UserOut(username=user.username, name=user.name, role=user.role, role_label=user.role_label,
                   is_admin=user.is_admin, modules=list(user.modules), ai=ai,
                   warehouse_ids=list(user.warehouse_ids), branch_ids=list(user.branch_ids),
                   warehouse_id=user.warehouse_id, branch_id=user.branch_id)


@router.get("/config", response_model=AuthConfig)
async def auth_config(settings: Settings = Depends(get_settings)) -> AuthConfig:
    """Público: lo que la pantalla de login necesita (el Client ID de Google no es secreto)."""
    return AuthConfig(google_client_id=settings.GOOGLE_CLIENT_ID or None, allowed_domain=settings.ALLOWED_EMAIL_DOMAIN,
                      password_login=settings.password_login)


@router.post("/google", response_model=TokenOut)
async def login_google(body: GoogleIn, request: Request, cache: Cache = Depends(get_cache)) -> TokenOut:
    await _rate_limit(request, cache)
    base = get_settings()
    settings = runtime_settings()
    store = get_store(base.HISTORY_DB_PATH)
    try:
        ident = await verify_google_credential(body.credential, base.GOOGLE_CLIENT_ID, base.ALLOWED_EMAIL_DOMAIN)
    except GoogleAuthError as exc:
        store.log("(desconocido)", "login_rechazado", _ip(request), str(exc))
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    rec = store.user(ident.email)
    if rec is None and ident.email not in base.admin_emails:
        # Primer ingreso de alguien del dominio: queda registrado para que un admin lo apruebe (o se aprueba solo).
        auto = settings.NEW_USER_AUTO_APPROVE and store.role(settings.NEW_USER_DEFAULT_ROLE) is not None
        store.upsert_user(UserRec(email=ident.email, name=ident.name, role=settings.NEW_USER_DEFAULT_ROLE if auto else None,
                                  status="activo" if auto else "pendiente"))
        store.log(ident.email, "registro", _ip(request), "aprobado automáticamente" if auto else "pendiente de aprobación")
    user, reason = google_user(ident.email)
    if not user:
        store.log(ident.email, "login_denegado", _ip(request), reason)
        raise HTTPException(status.HTTP_403_FORBIDDEN, reason)
    if rec is None and ident.email in base.admin_emails:  # admin de arranque: queda dado de alta en el panel
        store.upsert_user(UserRec(email=ident.email, name=ident.name, role="admin", status="activo"))
    store.touch_login(ident.email, ident.name)
    store.log(ident.email, "login", _ip(request), "google")
    token, expires = create_token(user)
    return TokenOut(access_token=token, expires_in=expires, user=_out(user, settings))


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, cache: Cache = Depends(get_cache)) -> TokenOut:
    """Acceso de emergencia con usuario/contraseña del .env (desactivado cuando se usa Google)."""
    settings = runtime_settings()
    if not settings.password_login:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Inicia sesión con tu cuenta de Google @{settings.ALLOWED_EMAIL_DOMAIN}")
    await _rate_limit(request, cache)
    store = get_store(settings.HISTORY_DB_PATH)
    user = authenticate(body.username, body.password)
    if not user:
        store.log(body.username, "login_rechazado", _ip(request), "contraseña")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario o contraseña incorrectos")
    store.log(body.username, "login", _ip(request), "contraseña")
    token, expires = create_token(user)
    return TokenOut(access_token=token, expires_in=expires, user=_out(user, settings))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> UserOut:
    return _out(user, runtime_settings())
