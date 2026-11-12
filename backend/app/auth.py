"""Autenticación y permisos.

Dos orígenes de usuario:
- **Google** (principal): cuentas @ALLOWED_EMAIL_DOMAIN. Se administran en el panel (rol, sucursales, almacenes,
  estado). Los correos de ADMIN_EMAILS entran como admin aunque no existan todavía.
- **Local** (emergencia): AUTH_USERS en .env, ``usuario|rol|hash[|almacen=ID;sucursal=ID]``; rol ``admin`` o
  ``viewer``. Solo funciona si el login con contraseña está habilitado (ALLOW_PASSWORD_LOGIN).

Los permisos se leen en CADA petición (no viven en el token): un cambio en el panel aplica de inmediato.
Generar un hash:  python -m app.auth hash "MiContraseña"
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .store import MODULES, get_store

_ITER = 600_000
_bearer = HTTPBearer(auto_error=False)
ENV_ROLE = {"admin": "admin", "viewer": "direccion"}  # roles del .env -> roles del panel


@dataclass(frozen=True)
class User:
    username: str  # correo (Google) o usuario local
    role: str
    role_label: str = ""
    name: str = ""
    is_admin: bool = False
    modules: tuple[str, ...] = tuple(MODULES)
    ai: bool = True
    # Alcance: si hay valores, el usuario SOLO ve esas sucursales / almacenes (se fuerza en el backend).
    warehouse_ids: tuple[int, ...] = ()
    branch_ids: tuple[int, ...] = ()
    source: str = "local"

    @property
    def scoped(self) -> bool:
        return bool(self.warehouse_ids or self.branch_ids)

    # Compatibilidad: primer almacén / sucursal del alcance
    @property
    def warehouse_id(self) -> int | None:
        return self.warehouse_ids[0] if len(self.warehouse_ids) == 1 else None

    @property
    def branch_id(self) -> int | None:
        return self.branch_ids[0] if len(self.branch_ids) == 1 else None

    def can(self, module: str) -> bool:
        return self.is_admin or module in self.modules

    def scope_filters(self, filters: Any) -> Any:
        """Aplica el alcance del usuario a un KpiFilters. Lo que pida el cliente fuera de su alcance se ignora."""
        if not self.scoped:
            return filters
        update: dict[str, Any] = {"strict_scope": True}
        for ids, single, allowed in ((self.warehouse_ids, "warehouse_id", "allowed_warehouse_ids"),
                                     (self.branch_ids, "branch_id", "allowed_branch_ids")):
            if not ids:
                continue
            requested = getattr(filters, single)
            if requested in ids:
                update[single], update[allowed] = requested, None
            elif len(ids) == 1:
                update[single], update[allowed] = ids[0], None
            else:
                update[single], update[allowed] = None, list(ids)
        return filters.model_copy(update=update)


def hash_password(password: str, iterations: int = _ITER) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2-sha256.{iterations}.{salt}.{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split(".")
        if algo != "pbkdf2-sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ usuarios locales (.env)
@dataclass(frozen=True)
class _Entry:
    role: str
    pw_hash: str
    warehouse_ids: tuple[int, ...] = ()
    branch_ids: tuple[int, ...] = ()


def _parse_scope(text: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """'almacen=3' · 'almacen=3+4;sucursal=7' -> ((3, 4), (7,))."""
    wh: list[int] = []
    br: list[int] = []
    for part in text.split(";"):
        k, _, v = part.partition("=")
        for x in v.replace("+", " ").split():
            if x.isdigit():
                if k.strip().lower() in ("almacen", "almacén", "warehouse"):
                    wh.append(int(x))
                elif k.strip().lower() in ("sucursal", "branch"):
                    br.append(int(x))
    return tuple(wh), tuple(br)


def _users() -> dict[str, _Entry]:
    out: dict[str, _Entry] = {}
    for item in get_settings().AUTH_USERS.split(","):
        parts = [p.strip() for p in item.strip().split("|")]
        if len(parts) not in (3, 4):
            continue
        user, role, pw_hash = parts[:3]
        wh, br = _parse_scope(parts[3]) if len(parts) == 4 else ((), ())
        if role in ENV_ROLE:
            out[user] = _Entry(role, pw_hash, wh, br)
    return out


def _build(username: str, role_name: str | None, *, name: str = "", wh: tuple[int, ...] = (), br: tuple[int, ...] = (),
           source: str) -> User | None:
    store = get_store(get_settings().HISTORY_DB_PATH)
    role = store.role(role_name)
    if not role:
        return None
    return User(username=username, role=role.name, role_label=role.label, name=name, is_admin=role.admin,
                modules=tuple(MODULES if role.admin else role.modules), ai=role.ai or role.admin,
                warehouse_ids=() if role.admin else wh, branch_ids=() if role.admin else br, source=source)


def _local_user(username: str) -> User | None:
    e = _users().get(username)
    return _build(username, ENV_ROLE[e.role], wh=e.warehouse_ids, br=e.branch_ids, source="local") if e else None


def google_user(email: str) -> tuple[User | None, str]:
    """(usuario, motivo si no tiene acceso)."""
    s = get_settings()
    rec = get_store(s.HISTORY_DB_PATH).user(email)
    if email.lower() in s.admin_emails and (not rec or rec.status != "bloqueado"):
        return _build(email, "admin", name=rec.name if rec else "", source="google"), ""
    if not rec:
        return None, "Tu cuenta no está registrada"
    if rec.status == "bloqueado":
        return None, "Tu acceso fue bloqueado por un administrador"
    if rec.status != "activo" or not rec.role:
        return None, "Tu acceso está pendiente de aprobación por un administrador"
    user = _build(email, rec.role, name=rec.name, wh=tuple(rec.warehouse_ids), br=tuple(rec.branch_ids), source="google")
    return (user, "") if user else (None, "Tu rol ya no existe; pide a un administrador que te asigne uno")


# Hash ficticio para igualar tiempos cuando el usuario no existe.
_DUMMY = hash_password("x", iterations=_ITER)


def authenticate(username: str, password: str) -> User | None:
    entry = _users().get(username)
    ok = verify_password(password, entry.pw_hash if entry else _DUMMY)
    return _local_user(username) if (entry and ok) else None


def create_token(user: User) -> tuple[str, int]:
    s = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(minutes=s.JWT_EXPIRE_MINUTES)
    token = jwt.encode({"sub": user.username, "src": user.source, "exp": exp, "iss": "prosic-bi"}, s.JWT_SECRET,
                       algorithm="HS256")
    return token, s.JWT_EXPIRE_MINUTES * 60


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    if not creds or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado", headers={"WWW-Authenticate": "Bearer"})
    s = get_settings()
    try:
        payload = jwt.decode(creds.credentials, s.JWT_SECRET, algorithms=["HS256"], issuer="prosic-bi")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida o expirada",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    username = str(payload.get("sub") or "")
    if payload.get("src") == "google":
        user, reason = google_user(username)
    else:
        user, reason = (_local_user(username) if s.password_login else None), "Usuario no válido"
    if not user:  # bloqueado, eliminado o sin rol: la sesión se corta de inmediato
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, reason or "Usuario no válido")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requiere rol de administrador")
    return user


def require_module(user: User, module: str) -> None:
    if not user.can(module):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu rol no tiene acceso a este módulo")


def require_ai(user: User) -> None:
    if not user.ai:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu rol no tiene acceso al asistente de IA")


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print('Uso: python -m app.auth hash "MiContraseña"')
