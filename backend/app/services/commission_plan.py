"""Comisiones — Fases 02 a 04: tablas de cálculo (plan), metas mensuales, cálculo por vendedor y comparativo.

Fase 02 (tablas dimensionales, se capturan en Administración → Comisiones):
- Puestos con su tabulador: % de comisión según el % de cumplimiento de la meta del mes.
  Un puesto comisiona sobre su base propia (ejecutivo, asesor) o sobre la de toda su sucursal (gerente).
- Vendedores (usuario vendedor de la factura en Odoo) con su puesto, sucursal y correo.
- Sucursales con los almacenes que miden su ocupación física.
- Factor de ocupación (opcional): multiplica la comisión según el % de ocupación de la sucursal en el mes.
- Metas mensuales por vendedor y por sucursal.

Fase 03 (cálculo, por mes calendario):
    base       = facturas cobradas al 100 % en el mes (Fase 01)
    cumplimiento = base / meta × 100
    % comisión = tramo del tabulador del puesto (el mayor «desde» que no rebase el cumplimiento)
    comisión   = base × % comisión × factor de ocupación + ajustes manuales

Fase 04 (comparativo): Finanzas captura su cálculo manual del mes y el BI muestra la diferencia.

Todo vive en la base local del BI (SQLite); nada se escribe en Odoo. Las cifras del tabulador las define la
empresa: el BI no trae porcentajes de fábrica (sin tabulador la comisión es 0 y se marca «Falta tabulador»).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def fix_text(v: str) -> str:
    """Repara texto UTF-8 que se guardó leído como Latin-1 («MuÃ±oz» -> «Muñoz»). Si no aplica, lo deja igual."""
    if not isinstance(v, str) or not any(c in v for c in "ÃÂ"):
        return v
    for enc in ("cp1252", "latin-1"):
        try:
            return v.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return v


# ------------------------------------------------------------------ modelos del plan (Fase 02)
class Tramo(BaseModel):
    desde: float = Field(ge=0, le=1000)  # % de cumplimiento de meta (o % de ocupación) a partir del cual aplica
    valor: float = Field(ge=0, le=100)  # % de comisión (tabulador) o factor (ocupación)


def _sorted_tramos(v: list[Tramo]) -> list[Tramo]:
    seen: set[float] = set()
    for t in v:
        if t.desde in seen:
            raise ValueError(f"Hay dos tramos que inician en {t.desde:g}")
        seen.add(t.desde)
    return sorted(v, key=lambda t: t.desde)


ACCOUNT_RE = r"^[0-9%-]{3,20}$"


class Puesto(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    label: str = Field(min_length=1, max_length=60)
    base: Literal["propia", "sucursal"] = "propia"
    tramos: list[Tramo] = Field(default_factory=list, max_length=30)
    # Cuentas de ingreso propias del puesto (p. ej. gerentes sobre todo); None = las generales de la depuración.
    cuentas: list[str] | None = Field(default=None, max_length=50)

    _t = field_validator("tramos")(lambda cls, v: _sorted_tramos(v))
    _l = field_validator("label")(lambda cls, v: fix_text(v))

    @field_validator("cuentas")
    @classmethod
    def _cuentas(cls, v: list[str] | None) -> list[str] | None:
        return _check_accounts(v) if v else None


class Vendedor(BaseModel):
    odoo_id: int = Field(ge=1)  # res.users del vendedor de la factura (invoice_user_id)
    nombre: str = Field(min_length=1, max_length=120)
    puesto: str
    sucursal_id: int | None = None
    correo: str = Field(default="", max_length=200)
    activo: bool = True

    _n = field_validator("nombre")(lambda cls, v: fix_text(v))

    @field_validator("correo")
    @classmethod
    def _correo(cls, v: str) -> str:
        v = v.strip().lower()
        if v and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError(f"Correo inválido: {v}")
        return v


class Sucursal(BaseModel):
    branch_id: int = Field(ge=1)
    nombre: str = Field(min_length=1, max_length=120)
    almacenes: list[int] = Field(default_factory=list)

    _n = field_validator("nombre")(lambda cls, v: fix_text(v))


class Ocupacion(BaseModel):
    activo: bool = False
    tramos: list[Tramo] = Field(default_factory=list, max_length=30)  # desde = % de ocupación; valor = factor

    _t = field_validator("tramos")(lambda cls, v: _sorted_tramos(v))


def _check_accounts(v: list[str]) -> list[str]:
    out = []
    for c in v:
        c = c.strip()
        if not re.match(ACCOUNT_RE, c):
            raise ValueError(f"Cuenta inválida: {c!r} (usa el código, p. ej. 401-001-001)")
        if c not in out:
            out.append(c)
    return out


class Regla(BaseModel):
    """Regla de saneamiento sobre las facturas cobradas antes de calcular.

    Condiciones (todas las capturadas deben cumplirse; las vacías no filtran), sobre los datos ORIGINALES de Odoo:
    vendedor y sucursal de la factura, texto del cliente o del centro de costo, folios y rango de fecha de cobro.
    Acciones:
    - ``reasignar``: la comisión pasa a otro vendedor (y/o sucursal), total o un porcentaje (traspasos de cartera,
      bajas de personal, cuentas compartidas). Aplica la primera regla de reasignación que coincida.
    - ``excluir``: la factura no comisiona (con el motivo de la regla).
    - ``incluir``: comisiona aunque la depuración automática la haya excluido (póliza mixta, cobro tardío…).
      No rescata cancelaciones con nota de crédito ni pólizas sin cobro real.
    """
    id: str = Field(pattern=r"^[a-z0-9]{4,16}$")
    activo: bool = True
    accion: Literal["reasignar", "excluir", "incluir"]
    motivo: str = Field(min_length=3, max_length=200)
    vendedor_id: int | None = Field(default=None, ge=1)
    sucursal_id: int | None = Field(default=None, ge=1)
    cliente: str = Field(default="", max_length=120)
    centro_costo: str = Field(default="", max_length=120)
    facturas: list[str] = Field(default_factory=list, max_length=500)
    desde: date | None = None  # fecha de cobro (saldada)
    hasta: date | None = None
    destino_vendedor_id: int | None = Field(default=None, ge=1)
    destino_sucursal_id: int | None = Field(default=None, ge=1)
    porcentaje: float = Field(default=100, gt=0, le=100)  # reasignar: % de la base que pasa al destino

    @field_validator("facturas")
    @classmethod
    def _facturas(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x.strip()]

    @model_validator(mode="after")
    def _check(self) -> "Regla":
        if not (self.vendedor_id or self.sucursal_id or self.cliente.strip() or self.centro_costo.strip()
                or self.facturas or self.desde or self.hasta):
            raise ValueError(f"Regla «{self.motivo}»: captura al menos una condición (vendedor, sucursal, cliente, "
                             "centro de costo, facturas o fechas)")
        if self.desde and self.hasta and self.hasta < self.desde:
            raise ValueError(f"Regla «{self.motivo}»: la fecha «hasta» es anterior a «desde»")
        if self.accion == "reasignar":
            if not (self.destino_vendedor_id or self.destino_sucursal_id):
                raise ValueError(f"Regla «{self.motivo}»: indica a qué vendedor o sucursal se traspasa")
            if self.destino_vendedor_id and self.destino_vendedor_id == self.vendedor_id and not self.destino_sucursal_id:
                raise ValueError(f"Regla «{self.motivo}»: el vendedor destino es el mismo que el original")
        return self


class Depuracion(BaseModel):
    """Reglas de saneamiento de datos que se aplican a las facturas cobradas antes del cálculo (Fase 03)."""
    # Cuentas de ingreso cuyo importe comisiona (códigos o patrones con %). Vacío = toda la lista de Finanzas.
    cuentas: list[str] = Field(default_factory=list, max_length=50)
    # Notas de crédito registradas en cuentas que no se eligieron (p. ej. 402-001-001 «Renta - NC»):
    # prorrateo = se descuenta la parte proporcional al ingreso que comisiona; todas = se descuentan completas;
    # ninguna = no se descuentan.
    notas_credito: Literal["prorrateo", "todas", "ninguna"] = "prorrateo"
    max_dias: int = Field(default=365, ge=1, le=3650)  # días máximos entre emisión y cobro total
    reglas: list[Regla] = Field(default_factory=list, max_length=300)

    @field_validator("cuentas")
    @classmethod
    def _cuentas(cls, v: list[str]) -> list[str]:
        return _check_accounts(v)

    @model_validator(mode="after")
    def _ids(self) -> "Depuracion":
        ids = [r.id for r in self.reglas]
        if len(ids) != len(set(ids)):
            raise ValueError("Hay reglas con el mismo identificador")
        return self


class Plan(BaseModel):
    puestos: list[Puesto] = Field(min_length=1, max_length=20)
    vendedores: list[Vendedor] = Field(default_factory=list, max_length=500)
    sucursales: list[Sucursal] = Field(default_factory=list, max_length=200)
    ocupacion: Ocupacion = Field(default_factory=Ocupacion)
    depuracion: Depuracion = Field(default_factory=Depuracion)

    @model_validator(mode="after")
    def _check(self) -> "Plan":
        keys = [p.key for p in self.puestos]
        if len(keys) != len(set(keys)):
            raise ValueError("Hay puestos repetidos")
        ids = [v.odoo_id for v in self.vendedores]
        if len(ids) != len(set(ids)):
            raise ValueError("Hay vendedores repetidos")
        for v in self.vendedores:
            if v.puesto not in keys:
                raise ValueError(f"{v.nombre}: el puesto «{v.puesto}» no existe")
            p = next(p for p in self.puestos if p.key == v.puesto)
            if p.base == "sucursal" and not v.sucursal_id:
                raise ValueError(f"{v.nombre}: su puesto comisiona por sucursal; asígnale una sucursal")
        if len({s.branch_id for s in self.sucursales}) != len(self.sucursales):
            raise ValueError("Hay sucursales repetidas")
        for r in self.depuracion.reglas:
            if r.destino_vendedor_id and r.destino_vendedor_id not in ids:
                raise ValueError(f"Regla «{r.motivo}»: el vendedor destino debe estar dado de alta en el plan")
        return self

    def puesto(self, key: str) -> Puesto | None:
        return next((p for p in self.puestos if p.key == key), None)

    def vendedor(self, odoo_id: int | None) -> Vendedor | None:
        return next((v for v in self.vendedores if v.odoo_id == odoo_id), None) if odoo_id else None

    def sucursal(self, branch_id: int | None) -> Sucursal | None:
        return next((s for s in self.sucursales if s.branch_id == branch_id), None) if branch_id else None


DEFAULT_PLAN = Plan(puestos=[
    Puesto(key="ejecutivo", label="Ejecutivo de ventas"),
    Puesto(key="asesor", label="Asesor comercial"),
    Puesto(key="gerente", label="Gerente de sucursal", base="sucursal"),
])


class Ajuste(BaseModel):
    odoo_id: int = Field(ge=1)
    monto: float = Field(ge=-10_000_000, le=10_000_000)
    motivo: str = Field(min_length=3, max_length=200)


class MesDatos(BaseModel):
    """Lo que se captura cada mes: metas, cálculo manual de Finanzas (Fase 04) y ajustes."""
    metas_vendedor: dict[int, float] = Field(default_factory=dict)  # odoo_id -> meta de base cobrada
    metas_sucursal: dict[int, float] = Field(default_factory=dict)  # branch_id -> meta
    manual: dict[int, float] = Field(default_factory=dict)  # odoo_id -> comisión calculada a mano por Finanzas
    ajustes: list[Ajuste] = Field(default_factory=list, max_length=200)

    @field_validator("metas_vendedor", "metas_sucursal", "manual")
    @classmethod
    def _non_negative(cls, v: dict[int, float]) -> dict[int, float]:
        if any(x < 0 or x > 1e10 for x in v.values()):
            raise ValueError("Los montos deben ser positivos")
        return {k: float(x) for k, x in v.items()}


# ------------------------------------------------------------------ almacenamiento
_SCHEMA = """
CREATE TABLE IF NOT EXISTS commission_plan (id INTEGER PRIMARY KEY CHECK (id = 1), doc TEXT NOT NULL,
    updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commission_month (month TEXT PRIMARY KEY, doc TEXT NOT NULL, updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commission_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PlanStore:
    def __init__(self, path: str):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _bump(self) -> None:  # llamado dentro de una transacción
        self._conn.execute("INSERT INTO commission_meta VALUES ('version', '1') ON CONFLICT(k) DO UPDATE SET "
                           "v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)")

    def version(self) -> int:
        r = self._one("SELECT v FROM commission_meta WHERE k = 'version'")
        return int(r["v"]) if r else 0

    def plan(self) -> Plan:
        r = self._one("SELECT doc FROM commission_plan WHERE id = 1")
        return Plan.model_validate_json(r["doc"]) if r else DEFAULT_PLAN.model_copy(deep=True)

    def plan_info(self) -> dict[str, Any]:
        r = self._one("SELECT updated_at, updated_by FROM commission_plan WHERE id = 1")
        return {"updated_at": r["updated_at"] if r else None, "updated_by": r["updated_by"] if r else None,
                "version": self.version()}

    def save_plan(self, plan: Plan, by: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO commission_plan VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                               "doc = excluded.doc, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                               (plan.model_dump_json(), _now(), by))
            self._bump()

    def month(self, month: str) -> MesDatos:
        r = self._one("SELECT doc FROM commission_month WHERE month = ?", (month,))
        return MesDatos.model_validate_json(r["doc"]) if r else MesDatos()

    def month_info(self, month: str) -> dict[str, Any]:
        r = self._one("SELECT updated_at, updated_by FROM commission_month WHERE month = ?", (month,))
        return {"updated_at": r["updated_at"] if r else None, "updated_by": r["updated_by"] if r else None}

    def save_month(self, month: str, data: MesDatos, by: str) -> None:
        if not MONTH_RE.match(month):
            raise ValueError("Mes inválido (AAAA-MM)")
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO commission_month VALUES (?, ?, ?, ?) ON CONFLICT(month) DO UPDATE SET "
                               "doc = excluded.doc, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                               (month, data.model_dump_json(), _now(), by))
            self._bump()

    def get(self, k: str) -> str | None:
        r = self._one("SELECT v FROM commission_meta WHERE k = ?", (k,))
        return r["v"] if r else None

    def set(self, k: str, v: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO commission_meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                               (k, v))

    def clear(self) -> None:  # pruebas
        with self._lock, self._conn:
            for t in ("commission_plan", "commission_month", "commission_meta"):
                self._conn.execute(f"DELETE FROM {t}")


@lru_cache(maxsize=4)
def get_plan_store(path: str) -> PlanStore:
    return PlanStore(path)


# ------------------------------------------------------------------ cálculo (Fase 03)
def tramo(tramos: list[Tramo], x: float | None) -> Tramo | None:
    """Tramo que aplica: el de mayor «desde» que no rebase ``x``."""
    if x is None:
        return None
    hit = None
    for t in sorted(tramos, key=lambda t: t.desde):
        if x + 1e-9 >= t.desde:
            hit = t
    return hit


def months_between(d0: date, d1: date) -> list[str]:
    out, y, m = [], d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_range(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    return start, end


def month_label(month: str) -> str:
    return f"{MESES[int(month[5:7]) - 1]} {month[:4]}"


OccupancyFn = Callable[[list[int], str], "float | None"]

# ------------------------------------------------------------------ depuración (reglas de saneamiento)
HARD_EXCLUSIONS = {"nc_100", "poliza"}  # no hubo cobro real: ninguna regla las rescata
AUTO_REASONS = {
    "nc_100": "Cancelada con nota de crédito (100 %)",
    "poliza": "Saldada con póliza de diario (cancelación / ajuste, no es cobro)",
    "mixta": "Saldada en parte con póliza de diario: revisar",
}


def _norm(s: Any) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _match_account(code: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(code, p.replace("%", "*")) for p in patterns)


def _is_credit_note_account(code: str) -> bool:
    from .. import odoo_mapping as M
    return _match_account(code, M.COMMISSION_CREDIT_NOTE_ACCOUNTS)


def base_for(r: dict[str, Any], patterns: list[str], nc_mode: str) -> float:
    """Base comisionable de una factura (ya multiplicada por su participación) con las cuentas elegidas.

    ``cuentas``: neto por cuenta (ingreso de la factura + notas de crédito aplicadas); ``ingreso``: solo la factura.
    Sin desglose por cuenta (datos antiguos) o sin cuentas elegidas, se usa la base completa."""
    share = r.get("_share", 1.0)
    cuentas = r.get("cuentas")
    if not patterns or cuentas is None:
        return r["base"] * share
    ingreso = r.get("ingreso") or {}
    sel = {c for c in cuentas if _match_account(c, patterns)}
    direct = sum(cuentas[c] for c in sel)
    nc_free = sum(v for c, v in cuentas.items() if c not in sel and _is_credit_note_account(c))
    inc_sel = sum(ingreso.get(c, 0.0) for c in sel)
    inc_all = sum(ingreso.values())
    if nc_mode == "prorrateo":
        direct += nc_free * (inc_sel / inc_all if inc_all else 0.0)
    elif nc_mode == "todas" and inc_sel:
        direct += nc_free
    return direct * share


def _rule_matches(rule: Regla, r: dict[str, Any]) -> bool:
    if rule.vendedor_id and r.get("vendedor_id") != rule.vendedor_id:
        return False
    if rule.sucursal_id and r.get("sucursal_id") != rule.sucursal_id:
        return False
    if rule.cliente.strip() and _norm(rule.cliente) not in _norm(r.get("cliente")):
        return False
    if rule.centro_costo.strip() and _norm(rule.centro_costo) not in _norm(r.get("centro_costo")):
        return False
    if rule.facturas and _norm(r.get("factura")) not in {_norm(f) for f in rule.facturas}:
        return False
    cobro = (r.get("fecha_cobro") or "")[:10]
    if rule.desde and (not cobro or cobro < rule.desde.isoformat()):
        return False
    if rule.hasta and (not cobro or cobro > rule.hasta.isoformat()):
        return False
    return True


def depurar(plan: Plan, invoices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aplica la depuración del plan a las facturas cobradas al 100 % (incluidas y excluidas por la Fase 01).

    Devuelve una fila por factura y beneficiario (una factura repartida al 50 % produce dos filas) con el
    vendedor/sucursal original y final, el estado (comisiona o no), el motivo y las reglas aplicadas."""
    dep = plan.depuracion
    rules = [r for r in dep.reglas if r.activo]
    branch_names: dict[int | None, str] = {s.branch_id: s.nombre for s in plan.sucursales}
    for r in invoices:
        if r.get("sucursal_id"):
            branch_names.setdefault(r["sucursal_id"], r.get("sucursal") or "")
    out: list[dict[str, Any]] = []
    for r in invoices:
        applied: list[str] = []
        motivo = ""
        mk = r.get("motivo_key")
        if mk in AUTO_REASONS:
            motivo = AUTO_REASONS[mk]
        elif r.get("dias") is not None and r["dias"] > dep.max_dias:
            motivo = f"Cobrada más de {dep.max_dias} días después de la emisión"
        excl = next((x for x in rules if x.accion == "excluir" and _rule_matches(x, r)), None)
        if excl:
            motivo = f"Regla: {excl.motivo}"
            applied.append(excl.motivo)
        elif motivo and mk not in HARD_EXCLUSIONS:
            inc = next((x for x in rules if x.accion == "incluir" and _rule_matches(x, r)), None)
            if inc:
                motivo = ""
                applied.append(inc.motivo)
        base_row = {**r, "vendedor_original": r.get("vendedor") or "Sin vendedor",
                    "vendedor_original_id": r.get("vendedor_id"), "sucursal_original_id": r.get("sucursal_id"),
                    "sucursal_original": r.get("sucursal") or "Sin sucursal",
                    "estado": "No comisiona" if motivo else "Comisiona", "motivo": motivo}
        rea = None if motivo else next((x for x in rules if x.accion == "reasignar" and _rule_matches(x, r)), None)
        parts: list[tuple[float, Regla | None]] = [(1.0, None)]
        if rea:
            p = rea.porcentaje / 100
            parts = [(p, rea)] + ([(1 - p, None)] if p < 1 else [])
        for share, rule in parts:
            row = {**base_row, "_share": share, "participacion": round(share * 100, 2), "regla": "; ".join(applied)}
            if rule:
                row["regla"] = "; ".join(applied + [rule.motivo])
                if rule.destino_vendedor_id:
                    v = plan.vendedor(rule.destino_vendedor_id)
                    row["vendedor_id"], row["vendedor"] = rule.destino_vendedor_id, v.nombre if v else str(rule.destino_vendedor_id)
                if rule.destino_sucursal_id:
                    row["sucursal_id"] = rule.destino_sucursal_id
                    row["sucursal"] = branch_names.get(rule.destino_sucursal_id) or str(rule.destino_sucursal_id)
                elif rule.destino_vendedor_id and (v := plan.vendedor(rule.destino_vendedor_id)) and v.sucursal_id:
                    row["sucursal_id"] = v.sucursal_id
                    row["sucursal"] = branch_names.get(v.sucursal_id) or row["sucursal"]
            row["base_comisionable"] = 0.0 if motivo else round(base_for(row, dep.cuentas, dep.notas_credito), 2)
            out.append(row)
    return out


def puesto_cuentas(plan: Plan, p: Puesto | None) -> list[str]:
    return (p.cuentas if p and p.cuentas else None) or plan.depuracion.cuentas


def calculate(plan: Plan, months: dict[str, MesDatos], invoices: list[dict[str, Any]], occupancy: OccupancyFn,
              today: date, clean: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Una fila por vendedor y mes. ``invoices``: facturas cobradas al 100 % (Fase 01), con
    ``fecha_cobro``, ``base``, ``vendedor_id``, ``vendedor``, ``sucursal_id`` y ``sucursal`` (y, si se tienen, el
    desglose por cuenta y el motivo de exclusión automática). ``clean``: el resultado de :func:`depurar`, si ya se tiene."""
    clean = clean if clean is not None else depurar(plan, invoices)
    nc_mode = plan.depuracion.notas_credito
    rows: list[dict[str, Any]] = []
    for month in sorted(months):
        data = months[month]
        inv = [r for r in clean if r["estado"] == "Comisiona" and (r.get("fecha_cobro") or "")[:7] == month]
        base_v: dict[int | None, float] = defaultdict(float)
        n_v: dict[int | None, int] = defaultdict(int)
        n_in: dict[int | None, int] = defaultdict(int)
        n_out: dict[int | None, int] = defaultdict(int)
        names: dict[int | None, str] = {}
        branch_of: dict[int | None, dict[int | None, float]] = defaultdict(lambda: defaultdict(float))
        branch_names: dict[int | None, str] = {}
        for r in inv:
            vid, bid = r.get("vendedor_id"), r.get("sucursal_id")
            v0 = plan.vendedor(vid)
            b = base_for(r, puesto_cuentas(plan, plan.puesto(v0.puesto) if v0 else None), nc_mode)
            base_v[vid] += b
            n_v[vid] += 1
            if vid != r.get("vendedor_original_id", vid):
                n_in[vid] += 1
                n_out[r.get("vendedor_original_id")] += 1
            names[vid] = r.get("vendedor") or "Sin vendedor"
            branch_of[vid][bid] += b
            branch_names[bid] = r.get("sucursal") or "Sin sucursal"
        for s in plan.sucursales:
            branch_names.setdefault(s.branch_id, s.nombre)

        def branch_base(bid: int | None, p: Puesto | None) -> float:
            pats = puesto_cuentas(plan, p)
            return sum(base_for(r, pats, nc_mode) for r in inv if r.get("sucursal_id") == bid)
        adj: dict[int, list[Ajuste]] = defaultdict(list)
        for a in data.ajustes:
            adj[a.odoo_id].append(a)
        start, end = month_range(month)
        preliminary = end >= today
        occ_cache: dict[int | None, float | None] = {}

        sellers = [v.odoo_id for v in plan.vendedores if v.activo]
        sellers += [vid for vid in base_v if vid not in sellers]
        sellers += [vid for vid in list(adj) + list(data.manual) if vid not in sellers]
        for vid in sellers:
            v = plan.vendedor(vid)
            obs: list[str] = []
            p = plan.puesto(v.puesto) if v else None
            own_bid = v.sucursal_id if v and v.sucursal_id else (
                max(branch_of[vid].items(), key=lambda kv: kv[1])[0] if branch_of.get(vid) else None)
            by_branch = bool(p and p.base == "sucursal")
            base = branch_base(own_bid, p) if by_branch else base_v.get(vid, 0.0)
            if n_in.get(vid):
                obs.append(f"Incluye {n_in[vid]} factura(s) traspasada(s) por regla")
            if n_out.get(vid):
                obs.append(f"{n_out[vid]} factura(s) traspasada(s) a otro vendedor")
            meta = data.metas_vendedor.get(vid) if vid else None
            if not meta and by_branch:
                meta = data.metas_sucursal.get(own_bid) if own_bid else None
            cumplimiento = base / meta * 100 if meta else None
            pct = 0.0
            if vid is None:
                obs.append("Facturas sin vendedor en Odoo")
            elif not v:
                obs.append("Vendedor no configurado en el plan")
            elif not v.activo:
                obs.append("Vendedor inactivo en el plan")
            elif not p or not p.tramos:
                obs.append("Falta tabulador del puesto")
            elif not meta and len(p.tramos) == 1 and p.tramos[0].desde == 0:
                pct = p.tramos[0].valor  # tasa fija: un solo tramo desde 0 % no depende de la meta
            elif not meta:
                obs.append("Falta meta del mes")
            else:
                t = tramo(p.tramos, cumplimiento)
                pct = t.valor if t else 0.0
                if not t:
                    obs.append("Cumplimiento debajo del primer tramo")
            bruta = base * pct / 100
            occ = factor = None
            if plan.ocupacion.activo and v and pct:
                suc = plan.sucursal(own_bid)
                if not suc or not suc.almacenes:
                    obs.append("Sucursal sin almacenes para medir ocupación (factor 1)")
                else:
                    if own_bid not in occ_cache:
                        occ_cache[own_bid] = occupancy(suc.almacenes, month)
                    occ = occ_cache[own_bid]
                    if occ is None:
                        obs.append("Sin datos de ocupación (factor 1)")
                    else:
                        t = tramo(plan.ocupacion.tramos, occ)
                        factor = t.valor if t else 0.0
            ajustes = sum(a.monto for a in adj.get(vid or 0, []))
            final = bruta * (factor if factor is not None else 1.0) + ajustes
            manual = data.manual.get(vid) if vid else None
            if preliminary:
                obs.append("Mes en curso: preliminar")
            if not base and not ajustes and manual is None and not (v and meta):
                continue  # sin actividad ni captura: no ocupa renglón
            rows.append({
                "mes": month, "mes_label": month_label(month),
                "vendedor_id": vid, "vendedor": v.nombre if v else names.get(vid, "Sin vendedor"),
                "puesto": p.label if p else "—", "sucursal_id": own_bid,
                "sucursal": branch_names.get(own_bid, "Sin sucursal") if own_bid is not None else "Sin sucursal",
                "base_tipo": "Sucursal" if by_branch else "Propia", "facturas": n_v.get(vid, 0),
                "base": round(base, 2), "meta": round(meta, 2) if meta else None,
                "cumplimiento": round(cumplimiento, 1) if cumplimiento is not None else None,
                "pct_comision": pct, "comision_bruta": round(bruta, 2),
                "ocupacion": round(occ, 1) if occ is not None else None, "factor": factor,
                "ajustes": round(ajustes, 2), "comision": round(final, 2),
                "manual": round(manual, 2) if manual is not None else None,
                "diferencia": round(final - manual, 2) if manual is not None else None,
                "correo": v.correo if v else "", "observaciones": "; ".join(obs),
                "_ajustes": [a.model_dump() for a in adj.get(vid or 0, [])],
            })
    return rows


def plan_to_json(plan: Plan) -> dict[str, Any]:
    return json.loads(plan.model_dump_json())
