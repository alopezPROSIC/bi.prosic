"""Usuarios, roles, configuración y bitácora de accesos (SQLite en el volumen /data).

Se administra desde el panel (/api/admin/*). Con ``HISTORY_DB_PATH`` vacío (pruebas) vive en memoria.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

MODULES = ["ventas", "rentas", "mantenimiento", "compras", "pronostico"]
STATUSES = ["activo", "pendiente", "bloqueado"]

# Roles iniciales (se pueden editar en el panel; «admin» siempre conserva todos los permisos).
DEFAULT_ROLES = [
    {"name": "admin", "label": "Administrador", "modules": MODULES, "ai": True, "admin": True},
    {"name": "direccion", "label": "Dirección", "modules": MODULES, "ai": True, "admin": False},
    {"name": "gerente", "label": "Gerente de sucursal", "modules": ["ventas", "rentas", "mantenimiento", "pronostico"],
     "ai": True, "admin": False},
    {"name": "consulta", "label": "Solo consulta", "modules": ["ventas", "rentas"], "ai": False, "admin": False},
]

# Parámetros que el panel puede cambiar sin reiniciar (sobrescriben al .env). Nada de credenciales aquí.
EDITABLE_SETTINGS: dict[str, type] = {
    "AI_ENABLED": bool,
    "AI_ANONYMIZE": bool,
    "AI_RATE_LIMIT_PER_MINUTE": int,
    "AI_RATE_LIMIT_PER_DAY": int,
    "KPI_CACHE_TTL_SECONDS": int,
    "SUMMARY_RECIPIENTS": str,
    "SUMMARY_WEEKDAY": int,
    "SUMMARY_HOUR": int,
    "ALERT_UTILIZATION_MIN_PCT": float,
    "ALERT_AR_OVERDUE_MAX_PCT": float,
    "ALERT_QUARANTINE_MAX_PCT": float,
    "NEW_USER_DEFAULT_ROLE": str,
    "NEW_USER_AUTO_APPROVE": bool,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    name TEXT PRIMARY KEY, label TEXT NOT NULL, modules TEXT NOT NULL, ai INTEGER NOT NULL, admin INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', role TEXT, status TEXT NOT NULL,
    branch_ids TEXT NOT NULL DEFAULT '[]', warehouse_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, last_login TEXT
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, email TEXT NOT NULL, event TEXT NOT NULL,
    ip TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS access_log_ts ON access_log(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Role:
    name: str
    label: str
    modules: list[str]
    ai: bool
    admin: bool


@dataclass
class UserRec:
    email: str
    name: str = ""
    role: str | None = None
    status: str = "pendiente"
    branch_ids: list[int] = field(default_factory=list)
    warehouse_ids: list[int] = field(default_factory=list)
    created_at: str = ""
    last_login: str | None = None


class Store:
    def __init__(self, path: str):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            for r in DEFAULT_ROLES:
                self._conn.execute("INSERT OR IGNORE INTO roles VALUES (?, ?, ?, ?, ?)",
                                   (r["name"], r["label"], json.dumps(r["modules"]), int(r["ai"]), int(r["admin"])))

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, args).rowcount

    # ---------------------------------------------------------------- roles
    def roles(self) -> list[Role]:
        return [Role(r["name"], r["label"], json.loads(r["modules"]), bool(r["ai"]), bool(r["admin"]))
                for r in self._q("SELECT * FROM roles ORDER BY admin DESC, name")]

    def role(self, name: str | None) -> Role | None:
        return next((r for r in self.roles() if r.name == name), None) if name else None

    def upsert_role(self, role: Role) -> None:
        if role.name == "admin":  # el rol admin no se puede degradar (evita quedarse sin administradores)
            role.modules, role.ai, role.admin = list(MODULES), True, True
        role.modules = [m for m in MODULES if m in role.modules]
        self._x("INSERT INTO roles VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET label=excluded.label, "
                "modules=excluded.modules, ai=excluded.ai, admin=excluded.admin",
                (role.name, role.label, json.dumps(role.modules), int(role.ai), int(role.admin)))

    def delete_role(self, name: str) -> None:
        if name == "admin":
            raise ValueError("El rol admin no se puede eliminar")
        if self._q("SELECT 1 FROM users WHERE role = ? LIMIT 1", (name,)):
            raise ValueError("Hay usuarios con este rol; reasígnalos antes de eliminarlo")
        self._x("DELETE FROM roles WHERE name = ?", (name,))

    # ---------------------------------------------------------------- usuarios
    @staticmethod
    def _user(r: sqlite3.Row) -> UserRec:
        return UserRec(r["email"], r["name"], r["role"], r["status"], json.loads(r["branch_ids"]),
                       json.loads(r["warehouse_ids"]), r["created_at"], r["last_login"])

    def users(self) -> list[UserRec]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY status, email")]

    def user(self, email: str) -> UserRec | None:
        rows = self._q("SELECT * FROM users WHERE email = ?", (email.lower(),))
        return self._user(rows[0]) if rows else None

    def upsert_user(self, u: UserRec) -> UserRec:
        u.email = u.email.strip().lower()
        if u.status not in STATUSES:
            raise ValueError(f"Estado inválido: {u.status}")
        if u.role and not self.role(u.role):
            raise ValueError(f"El rol «{u.role}» no existe")
        current = self.user(u.email)
        if current and self._is_last_admin(current) and (u.status != "activo" or not self._role_is_admin(u.role)):
            raise ValueError("No puedes quitar o bloquear al último administrador activo")
        self._x("INSERT INTO users (email, name, role, status, branch_ids, warehouse_ids, created_at, last_login) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET name=excluded.name, role=excluded.role, "
                "status=excluded.status, branch_ids=excluded.branch_ids, warehouse_ids=excluded.warehouse_ids",
                (u.email, u.name, u.role, u.status, json.dumps(sorted(set(u.branch_ids))),
                 json.dumps(sorted(set(u.warehouse_ids))), u.created_at or _now(), u.last_login))
        return self.user(u.email)  # type: ignore[return-value]

    def delete_user(self, email: str) -> None:
        current = self.user(email)
        if current and self._is_last_admin(current):
            raise ValueError("No puedes eliminar al último administrador activo")
        self._x("DELETE FROM users WHERE email = ?", (email.lower(),))

    def touch_login(self, email: str, name: str | None = None) -> None:
        if name:
            self._x("UPDATE users SET last_login = ?, name = CASE WHEN name = '' THEN ? ELSE name END WHERE email = ?",
                    (_now(), name, email.lower()))
        else:
            self._x("UPDATE users SET last_login = ? WHERE email = ?", (_now(), email.lower()))

    def _role_is_admin(self, role: str | None) -> bool:
        r = self.role(role)
        return bool(r and r.admin)

    def _is_last_admin(self, u: UserRec) -> bool:
        if u.status != "activo" or not self._role_is_admin(u.role):
            return False
        admins = [x for x in self.users() if x.status == "activo" and self._role_is_admin(x.role)]
        return len(admins) <= 1

    # ---------------------------------------------------------------- configuración
    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self._q("SELECT k, v FROM settings"):
            if r["k"] in EDITABLE_SETTINGS:
                out[r["k"]] = json.loads(r["v"])
        return out

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for k, v in values.items():
            if k not in EDITABLE_SETTINGS:
                raise ValueError(f"Parámetro no editable: {k}")
            if v is None:
                self._x("DELETE FROM settings WHERE k = ?", (k,))  # vuelve al valor del .env
                continue
            typ = EDITABLE_SETTINGS[k]
            try:
                v = typ(v) if typ is not bool else (v if isinstance(v, bool) else str(v).lower() in ("1", "true", "si", "sí"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Valor inválido para {k}") from exc
            if k == "NEW_USER_DEFAULT_ROLE" and not self.role(v):
                raise ValueError(f"El rol «{v}» no existe")
            self._x("INSERT INTO settings VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, json.dumps(v)))
        return self.settings()

    # ---------------------------------------------------------------- bitácora
    def log(self, email: str, event: str, ip: str | None = None, detail: str | None = None) -> None:
        self._x("INSERT INTO access_log (ts, email, event, ip, detail) VALUES (?, ?, ?, ?, ?)",
                (_now(), email.lower(), event, ip, (detail or "")[:300]))
        self._x("DELETE FROM access_log WHERE id IN (SELECT id FROM access_log ORDER BY id DESC LIMIT -1 OFFSET 20000)")

    def access_log(self, limit: int = 300, email: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT ts, email, event, ip, detail FROM access_log", ()
        if email:
            sql, args = sql + " WHERE email = ?", (email.lower(),)
        return [dict(r) for r in self._q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]


def role_dict(r: Role) -> dict[str, Any]:
    return asdict(r)


@lru_cache(maxsize=4)
def get_store(path: str) -> Store:
    return Store(path)
