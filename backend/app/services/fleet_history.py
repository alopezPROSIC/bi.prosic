"""Histórico diario de la flota de renta: estado de cada máquina al cierre de cada día.

Odoo NO guarda una foto diaria de la flota, pero sí registra cada cambio de estado de cada vehículo
(``fleet.vehicle.state_id`` tiene seguimiento → ``mail.tracking.value``) desde dic-2022, y cada movimiento
de inventario por número de serie (``stock.move.line``). Con eso se reconstruye, día por día:

- estado al cierre del día      → último cambio de ``state_id`` hasta ese día
- almacén al cierre del día     → último movimiento de la serie hacia una ubicación de un almacén
- si estaba activo (no archivado) → cambios del campo ``active`` (también con seguimiento)

Todo se copia a SQLite (tablas ``fh_*`` en ``HISTORY_DB_PATH``) y se sincroniza de forma incremental:
así el histórico queda guardado en una tabla propia aunque Odoo depure sus registros, y las consultas
del dashboard no tocan Odoo. Solo lectura sobre Odoo (search_read).

Flota de renta = vehículos cuyo producto tiene «Puede rentarse» (excluye autos utilitarios, tractos, etc.).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from ..config import Settings
from ..odoo_client import OdooClient

log = logging.getLogger("prosic.fleet_history")

# Nombres en español para los estados de Odoo (la llave es el nombre del estado en Odoo).
STATE_LABELS = {
    "available": "Disponible", "to deliver": "Por entregar", "rented": "Rentado",
    "rented - service": "Rentado - Servicio", "inspection": "Inspección",
    "service - garage": "Servicio - Taller", "to collect": "Por recolectar", "subasta": "Subasta",
}
PAGE = 10000
MAX_EXPORT_ROWS = 1_000_000  # Excel abre hasta 1,048,576 filas por hoja
SCHEMA_VERSION = "2"  # v2: atributos del equipo (familia, centro de costo, propietario…) y fecha de inicio del estado
# Tipo de combustible de Odoo → texto que se agrega a la familia para formar la subfamilia («… DIESEL»).
FUEL_LABELS = {
    "diesel": "DIESEL", "gasoline": "GASOLINA", "electric": "ELÉCTRICO", "lpg": "GAS LP", "cng": "GAS NATURAL",
    "hybrid": "HÍBRIDO DIESEL", "full_hybrid_gasoline": "HÍBRIDO GASOLINA", "hydrogen": "HIDRÓGENO",
    "plug_in_hybrid_diesel": "HÍBRIDO ENCHUFABLE DIESEL", "plug_in_hybrid_gasoline": "HÍBRIDO ENCHUFABLE GASOLINA",
}
# Columnas agregadas en v2 (las bases creadas antes las reciben con ALTER TABLE).
_V2_VEHICLE_COLS = {"plate": "TEXT NOT NULL DEFAULT ''", "serial_no": "TEXT NOT NULL DEFAULT ''",
                    "familia": "TEXT NOT NULL DEFAULT ''", "subfamilia": "TEXT NOT NULL DEFAULT ''",
                    "cost_center": "TEXT NOT NULL DEFAULT ''", "owner": "TEXT NOT NULL DEFAULT ''",
                    "subasta": "INTEGER NOT NULL DEFAULT 0", "state_date": "TEXT"}
_VEHICLE_COLS = ["id", "name", "serie", "lot_id", "product", "model", "rental", "created", "active", "state_id",
                 "warehouse_id", *_V2_VEHICLE_COLS]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fh_state (id INTEGER PRIMARY KEY, name TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fh_warehouse (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fh_vehicle (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, serie TEXT NOT NULL, lot_id INTEGER, product TEXT NOT NULL,
    model TEXT NOT NULL, rental INTEGER NOT NULL, created TEXT NOT NULL, active INTEGER NOT NULL,
    state_id INTEGER, warehouse_id INTEGER NOT NULL,
    plate TEXT NOT NULL DEFAULT '', serial_no TEXT NOT NULL DEFAULT '', familia TEXT NOT NULL DEFAULT '',
    subfamilia TEXT NOT NULL DEFAULT '', cost_center TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
    subasta INTEGER NOT NULL DEFAULT 0, state_date TEXT
);
CREATE TABLE IF NOT EXISTS fh_event (
    src INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
    old INTEGER, new INTEGER
);
CREATE INDEX IF NOT EXISTS fh_event_vehicle ON fh_event(vehicle_id, ts);
CREATE TABLE IF NOT EXISTS fh_move (src INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL, ts TEXT NOT NULL,
                                    warehouse_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fh_move_lot ON fh_move(lot_id, ts);
CREATE TABLE IF NOT EXISTS fh_daily (
    day TEXT NOT NULL, vehicle_id INTEGER NOT NULL, state_id INTEGER, warehouse_id INTEGER NOT NULL,
    since TEXT,  -- día en que empezó el estado vigente (date_duration)
    PRIMARY KEY (day, vehicle_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS fh_daily_wh ON fh_daily(warehouse_id, day);
CREATE TABLE IF NOT EXISTS fh_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def state_label(name: str) -> str:
    return STATE_LABELS.get(name.strip().lower(), name)


def _m2o(v: Any) -> int | None:
    return v[0] if isinstance(v, (list, tuple)) and v else None


def _m2o_name(v: Any) -> str:
    return str(v[1]) if isinstance(v, (list, tuple)) and len(v) > 1 else ""


class FleetStore:
    """Tablas ``fh_*`` en SQLite. Métodos síncronos: se llaman con ``asyncio.to_thread``."""

    def __init__(self, path: str):
        self.path = path
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self._conn() as c:
                c.executescript(_SCHEMA)
                _migrate(c)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    # ---------------------------------------------------------------- meta
    def meta(self) -> dict[str, str]:
        with self._conn() as c:
            return dict(c.execute("SELECT k, v FROM fh_meta").fetchall())

    def set_meta(self, **values: Any) -> None:
        with self._conn() as c:
            c.executemany("INSERT INTO fh_meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                          [(k, str(v)) for k, v in values.items()])

    # ---------------------------------------------------------------- escritura
    def save_catalogs(self, states: list[tuple], warehouses: list[tuple], vehicles: list[tuple]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM fh_state")
            c.executemany("INSERT INTO fh_state VALUES (?, ?, ?)", states)
            c.executemany("INSERT INTO fh_warehouse VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                          warehouses)
            cols = ", ".join(_VEHICLE_COLS)
            updates = ", ".join(f"{k}=excluded.{k}" for k in _VEHICLE_COLS[1:])
            c.executemany(f"INSERT INTO fh_vehicle ({cols}) VALUES ({', '.join('?' * len(_VEHICLE_COLS))}) "
                          f"ON CONFLICT(id) DO UPDATE SET {updates}", vehicles)

    def save_events(self, events: list[tuple]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO fh_event VALUES (?, ?, ?, ?, ?, ?)", events)

    def save_moves(self, moves: list[tuple]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO fh_move VALUES (?, ?, ?, ?)", moves)

    def rebuild(self, start: date, today: date, now_ts: str) -> int:
        """Recalcula ``fh_daily`` desde ``start`` hasta hoy (hoy = estado actual). Devuelve filas escritas."""
        with self._conn() as c:
            vehicles = c.execute("SELECT id, lot_id, created, active, state_id, warehouse_id, state_date "
                                 "FROM fh_vehicle").fetchall()
            ev: dict[tuple[int, str], list[tuple[str, int, int]]] = defaultdict(list)
            for vid, ts, kind, old, new in c.execute("SELECT vehicle_id, ts, kind, old, new FROM fh_event ORDER BY ts, src"):
                ev[(vid, kind)].append((ts, old, new))
            mv: dict[int, list[tuple[str, int]]] = defaultdict(list)
            for lot, ts, wh in c.execute("SELECT lot_id, ts, warehouse_id FROM fh_move ORDER BY ts, src"):
                mv[lot].append((ts, wh))

            def rows() -> Iterator[tuple]:
                for vid, lot, created, active, state_id, wh_now, state_date in vehicles:
                    yield from _vehicle_days(vid, created, ev.get((vid, "state"), []), ev.get((vid, "active"), []),
                                             mv.get(lot, []) if lot else [], state_id, bool(active), wh_now,
                                             start, today, now_ts, state_date)

            c.execute("DELETE FROM fh_daily WHERE day >= ?", (start.isoformat(),))
            before = c.total_changes
            c.executemany("INSERT INTO fh_daily VALUES (?, ?, ?, ?, ?)", rows())
            return c.total_changes - before

    # ---------------------------------------------------------------- lectura
    def states(self) -> list[tuple[int, str]]:
        with self._conn() as c:
            return c.execute("SELECT id, name FROM fh_state ORDER BY seq, id").fetchall()

    def warehouses(self) -> dict[int, str]:
        with self._conn() as c:
            return dict(c.execute("SELECT id, name FROM fh_warehouse").fetchall())

    def first_day(self) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT MIN(day) FROM fh_daily").fetchone()
            return row[0] if row else None

    @staticmethod
    def _where(d_from: date, d_to: date, wh_ids: list[int] | None) -> tuple[str, list[Any]]:
        sql = " FROM fh_daily d JOIN fh_vehicle v ON v.id = d.vehicle_id WHERE v.rental = 1 AND d.day BETWEEN ? AND ?"
        args: list[Any] = [d_from.isoformat(), d_to.isoformat()]
        if wh_ids:
            sql += f" AND d.warehouse_id IN ({','.join('?' * len(wh_ids))})"
            args += list(wh_ids)
        return sql, args

    def counts(self, d_from: date, d_to: date, wh_ids: list[int] | None) -> list[tuple[str, int, int, int]]:
        """(día, almacén, estado, unidades) de la flota de renta."""
        sql, args = self._where(d_from, d_to, wh_ids)
        with self._conn() as c:
            return c.execute("SELECT d.day, d.warehouse_id, d.state_id, COUNT(*)" + sql +
                             " GROUP BY d.day, d.warehouse_id, d.state_id", args).fetchall()

    # Registros del histórico (un renglón por equipo y día) para consultar, filtrar y exportar.
    _REC_COLS = ("d.day, v.name, v.plate, v.serial_no, v.serie, v.product, v.familia, v.subfamilia, "
                 "COALESCE(w.name, 'Sin almacén'), COALESCE(s.name, ''), d.state_id, v.cost_center, v.owner, "
                 "d.since, v.subasta")
    REC_KEYS = ("day", "name", "plate", "serial_no", "serie", "product", "familia", "subfamilia", "warehouse",
                "state", "state_id", "cost_center", "owner", "since", "subasta")
    _TEXT_FILTERS = {"familia": "v.familia", "subfamilia": "v.subfamilia", "cost_center": "v.cost_center",
                     "owner": "v.owner"}

    def _rec_where(self, d_from: date, d_to: date, wh_ids: list[int] | None, flt: dict[str, Any],
                   days: list[date] | None) -> tuple[str, list[Any]]:
        sql = (" FROM fh_daily d JOIN fh_vehicle v ON v.id = d.vehicle_id "
               "LEFT JOIN fh_warehouse w ON w.id = d.warehouse_id LEFT JOIN fh_state s ON s.id = d.state_id "
               "WHERE v.rental = 1")
        args: list[Any] = []
        if days is not None:
            sql += f" AND d.day IN ({','.join('?' * len(days)) or 'NULL'})"
            args += [d.isoformat() for d in days]
        else:
            sql += " AND d.day BETWEEN ? AND ?"
            args += [d_from.isoformat(), d_to.isoformat()]
        if wh_ids:
            sql += f" AND d.warehouse_id IN ({','.join('?' * len(wh_ids))})"
            args += list(wh_ids)
        if flt.get("state_id") is not None:
            sql += " AND d.state_id = ?"
            args.append(flt["state_id"])
        for key, col in self._TEXT_FILTERS.items():
            if flt.get(key):
                sql += f" AND {col} = ?"
                args.append(flt[key])
        if flt.get("subasta") is not None:
            sql += " AND v.subasta = ?"
            args.append(int(bool(flt["subasta"])))
        if q := (flt.get("q") or "").strip():
            like = f"%{q}%"
            sql += " AND (v.serie LIKE ? OR v.plate LIKE ? OR v.serial_no LIKE ? OR v.name LIKE ? OR v.product LIKE ?)"
            args += [like] * 5
        return sql, args

    def count_records(self, d_from: date, d_to: date, wh_ids: list[int] | None, flt: dict[str, Any],
                      days: list[date] | None = None) -> int:
        sql, args = self._rec_where(d_from, d_to, wh_ids, flt, days)
        with self._conn() as c:
            return c.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]

    def records(self, d_from: date, d_to: date, wh_ids: list[int] | None, flt: dict[str, Any],
                days: list[date] | None = None, limit: int | None = None, offset: int = 0,
                newest_first: bool = False) -> list[dict[str, Any]]:
        sql, args = self._rec_where(d_from, d_to, wh_ids, flt, days)
        sql = f"SELECT {self._REC_COLS}{sql} ORDER BY d.day{' DESC' if newest_first else ''}, w.name, v.serie"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        with self._conn() as c:
            return [dict(zip(self.REC_KEYS, r)) for r in c.execute(sql, args)]

    def options(self) -> dict[str, list[str]]:
        """Valores disponibles para los filtros del histórico (solo flota de renta)."""
        out: dict[str, list[str]] = {}
        with self._conn() as c:
            for key, col in self._TEXT_FILTERS.items():
                out[key] = [r[0] for r in c.execute(
                    f"SELECT DISTINCT {col.split('.')[1]} FROM fh_vehicle WHERE rental = 1 AND {col.split('.')[1]} <> '' "
                    "ORDER BY 1")]
        return out


def _vehicle_days(vid: int, created: str, state_ev: list, active_ev: list, moves: list, state_now: int | None,
                  active_now: bool, wh_now: int, start: date, today: date, now_ts: str,
                  state_date: str | None = None) -> Iterator[tuple]:
    """Filas (día, vehículo, estado, almacén, inicio del estado) desde ``start`` hasta hoy, al cierre de cada día.

    Inicio del estado = día del último cambio de estado; antes del primer cambio, el alta del equipo. Si el equipo
    nunca cambió de estado se usa la «Fecha de estado» que guarda Odoo (cuando ya había ocurrido ese día)."""
    first = max(start, date.fromisoformat(created[:10]))
    if first > today:
        return
    # Valor inicial = valor «anterior» del primer cambio; sin cambios = valor actual.
    state = state_ev[0][1] if state_ev else state_now
    since = created[:10]
    active = bool(active_ev[0][1]) if active_ev else active_now
    wh = moves[0][1] if moves else wh_now
    i_s = i_a = i_m = 0
    d = first
    while d <= today:
        end = now_ts if d == today else d.isoformat() + " 23:59:59"
        while i_s < len(state_ev) and state_ev[i_s][0] <= end:
            if state_ev[i_s][2] != state:
                since = state_ev[i_s][0][:10]
            state = state_ev[i_s][2]
            i_s += 1
        while i_a < len(active_ev) and active_ev[i_a][0] <= end:
            active = bool(active_ev[i_a][2])
            i_a += 1
        while i_m < len(moves) and moves[i_m][0] <= end:
            wh = moves[i_m][1]
            i_m += 1
        if active:
            day = d.isoformat()
            s = state_date if not state_ev and state_date and state_date[:10] <= day else since
            yield (day, vid, state, wh, s[:10])
        d += timedelta(days=1)


def _migrate(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(fh_vehicle)")}
    for col, decl in _V2_VEHICLE_COLS.items():
        if col not in have:
            c.execute(f"ALTER TABLE fh_vehicle ADD COLUMN {col} {decl}")
    if "since" not in {r[1] for r in c.execute("PRAGMA table_info(fh_daily)")}:
        c.execute("ALTER TABLE fh_daily ADD COLUMN since TEXT")


@lru_cache(maxsize=4)
def get_fleet_store(path: str) -> FleetStore:
    try:
        return FleetStore(path)
    except (OSError, sqlite3.Error) as exc:
        log.warning("Histórico de flota desactivado (%s): %s", path, exc)
        return FleetStore("")


# ==================================================================== sincronización con Odoo
_sync_lock = asyncio.Lock()


def _local(ts_utc: str, tz: ZoneInfo) -> str:
    dt = datetime.strptime(ts_utc[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


async def _paged(odoo: OdooClient, model: str, domain: list[Any], fields: list[str], cursor: int) -> list[dict[str, Any]]:
    """Todos los registros con id > cursor, en páginas (evita el tope ODOO_MAX_RECORDS)."""
    out: list[dict[str, Any]] = []
    while True:
        page = await odoo.search_read(model, domain + [("id", ">", cursor)], fields, order="id", limit=PAGE)
        out += page
        if len(page) < PAGE:
            return out
        cursor = page[-1]["id"]


async def _tracking(odoo: OdooClient, field_id: int, cursor: int, tz: ZoneInfo, kind: str) -> list[tuple]:
    rows = await _paged(odoo, "mail.tracking.value", [("field", "=", field_id)],
                        ["mail_message_id", "old_value_integer", "new_value_integer", "create_date"], cursor)
    msg_ids = sorted({_m2o(r["mail_message_id"]) for r in rows if _m2o(r["mail_message_id"])})
    res: dict[int, int] = {}
    for i in range(0, len(msg_ids), PAGE):
        chunk = msg_ids[i:i + PAGE]
        for m in await odoo.search_read("mail.message", [("id", "in", chunk), ("model", "=", "fleet.vehicle")],
                                        ["res_id"], limit=len(chunk)):
            res[m["id"]] = m["res_id"]
    return [(r["id"], res[mid], _local(r["create_date"], tz), kind, r["old_value_integer"], r["new_value_integer"])
            for r in rows if (mid := _m2o(r["mail_message_id"])) in res]


async def _location_warehouses(odoo: OdooClient) -> tuple[dict[int, int], list[tuple[int, str]]]:
    """Ubicación -> almacén (por la vista del almacén o su «Rental out Location»)."""
    fields = ["id", "name", "view_location_id"]
    wfields = await odoo.fields_get("stock.warehouse")
    if "rental_out_location_id" in wfields:
        fields.append("rental_out_location_id")
    whs = await odoo.search_read("stock.warehouse", [("active", "in", [True, False])], fields)
    locs = await odoo.search_read("stock.location", [("active", "in", [True, False]), ("usage", "=", "internal")],
                                  ["id", "parent_path"], limit=50000)
    out: dict[int, int] = {}
    for l in locs:
        path = f"/{l.get('parent_path') or ''}"
        w = next((w for w in whs if _m2o(w["view_location_id"]) and f"/{_m2o(w['view_location_id'])}/" in path), None)
        if w:
            out[l["id"]] = w["id"]
    for w in whs:
        if loc := _m2o(w.get("rental_out_location_id")):
            out[loc] = w["id"]
    return out, [(w["id"], w["name"]) for w in whs]


async def sync(odoo: OdooClient, settings: Settings) -> dict[str, Any]:
    """Trae de Odoo lo nuevo (incremental) y recalcula los días afectados. Seguro de llamar seguido."""
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    if not store.enabled:
        return {"enabled": False}
    if _sync_lock.locked():
        return {"enabled": True, "running": True}
    async with _sync_lock:
        t0 = time.monotonic()
        tz = ZoneInfo(settings.ODOO_TZ)
        meta = await asyncio.to_thread(store.meta)
        cur_state, cur_active, cur_move = (int(meta.get(k, 0)) for k in ("cursor_state", "cursor_active", "cursor_move"))

        # 1) catálogos y vehículos (incluye archivados)
        states = await odoo.search_read("fleet.vehicle.state", [], ["id", "name", "sequence"])
        loc_wh, whs = await _location_warehouses(odoo)
        vfields = await odoo.fields_get("fleet.vehicle")
        extra = [f for f in ("license_plate", "serial_no", "fuel_type", "cost_center_id", "owner_id", "subasta",
                             "state_date") if f in vfields]
        veh = await odoo.search_read("fleet.vehicle", [("active", "in", [True, False])],
                                     ["id", "name", "lot_id", "product_id", "model_id", "state_id", "active",
                                      "current_warehouse_id", "create_date", *extra], limit=100000)
        pids = sorted({p for v in veh if (p := _m2o(v["product_id"]))})
        products = {p["id"]: p for p in await odoo.search_read(
            "product.product", [("id", "in", pids), ("active", "in", [True, False])],
            ["id", "rental_ok", "default_code"], limit=len(pids) or 1)} if pids else {}
        vehicles = [_vehicle_row(v, products.get(_m2o(v["product_id"]) or 0, {}), tz) for v in veh]
        await asyncio.to_thread(store.save_catalogs, [(s["id"], s["name"], s["sequence"]) for s in states], whs, vehicles)

        # 2) cambios de estado y de archivado (seguimiento de Odoo)
        fids = {f["name"]: f["id"] for f in await odoo.search_read(
            "ir.model.fields", [("model", "=", "fleet.vehicle"), ("name", "in", ["state_id", "active"])], ["id", "name"])}
        events: list[tuple] = []
        if "state_id" in fids:
            events += await _tracking(odoo, fids["state_id"], cur_state, tz, "state")
        if "active" in fids:
            events += await _tracking(odoo, fids["active"], cur_active, tz, "active")
        await asyncio.to_thread(store.save_events, events)

        # 3) movimientos de inventario de las series de la flota (para saber en qué almacén estaba)
        #    Series nuevas (vehículo recién dado de alta) se traen completas; las conocidas, solo lo nuevo.
        lots = sorted({v[3] for v in vehicles if v[3]})
        known_lots = {int(x) for x in meta.get("lots", "").split(",") if x}
        moves: list[tuple] = []
        max_move = cur_move
        for group, cursor in (([l for l in lots if l in known_lots], cur_move), ([l for l in lots if l not in known_lots], 0)):
            for i in range(0, len(group), 2000):
                rows = await _paged(odoo, "stock.move.line",
                                    [("state", "=", "done"), ("lot_id", "in", group[i:i + 2000])],
                                    ["lot_id", "location_dest_id", "date"], cursor)
                max_move = max([max_move] + [r["id"] for r in rows])
                moves += [(r["id"], _m2o(r["lot_id"]), _local(r["date"], tz), loc_wh[loc]) for r in rows
                          if (loc := _m2o(r["location_dest_id"])) in loc_wh]
        await asyncio.to_thread(store.save_moves, moves)

        # 4) recalcular días: desde lo último construido o desde el cambio más antiguo recién llegado
        now = datetime.now(tz)
        today = now.date()
        starts = [date.fromisoformat(e[2][:10]) for e in events] + [date.fromisoformat(m[2][:10]) for m in moves]
        built = meta.get("built_until") if meta.get("schema") == SCHEMA_VERSION else None  # cambio de esquema: todo
        if built:
            starts.append(date.fromisoformat(built))
            known = set(int(x) for x in meta.get("vehicles", "").split(",") if x)
            starts += [date.fromisoformat(v[7][:10]) for v in vehicles if v[0] not in known]
        else:
            starts.append(min((date.fromisoformat(v[7][:10]) for v in vehicles), default=today))
        start = min(starts) if starts else today
        written = await asyncio.to_thread(store.rebuild, start, today, now.strftime("%Y-%m-%d %H:%M:%S"))

        new_meta: dict[str, Any] = {"built_until": today.isoformat(), "last_sync": now.isoformat(timespec="seconds"),
                                    "schema": SCHEMA_VERSION,
                                    "vehicles": ",".join(str(v[0]) for v in vehicles),
                                    "lots": ",".join(str(l) for l in lots), "cursor_move": max_move}
        for kind, key in (("state", "cursor_state"), ("active", "cursor_active")):
            ids = [e[0] for e in events if e[3] == kind]
            if ids:
                new_meta[key] = max(ids)
        await asyncio.to_thread(store.set_meta, **new_meta)
        result = {"enabled": True, "events": len(events), "moves": len(moves), "vehicles": len(vehicles),
                  "rebuilt_from": start.isoformat(), "rows": written, "seconds": round(time.monotonic() - t0, 1)}
        log.info("Histórico de flota sincronizado: %s", result)
        return result


def _short(name: str) -> str:
    return name.split("] ", 1)[1] if name.startswith("[") and "] " in name else name


def _code(name: str) -> str:
    return name[1:].split("] ", 1)[0] if name.startswith("[") and "] " in name else ""


def _vehicle_row(v: dict[str, Any], product: dict[str, Any], tz: ZoneInfo) -> tuple:
    """Fila de ``fh_vehicle`` (mismo orden que ``_VEHICLE_COLS``)."""
    pname = _m2o_name(v["product_id"])
    familia = " ".join((product.get("default_code") or _code(pname) or "").split())  # sin espacios duros (\xa0)
    fuel = FUEL_LABELS.get(v.get("fuel_type") or "", "")
    subfamilia = f"{familia}. {fuel}" if familia and fuel else familia
    return (v["id"], v["name"] or "", _m2o_name(v["lot_id"]), _m2o(v["lot_id"]), _short(pname),
            _m2o_name(v["model_id"]), int(bool(product.get("rental_ok"))), _local(v["create_date"], tz),
            int(bool(v["active"])), _m2o(v["state_id"]), _m2o(v["current_warehouse_id"]) or 0,
            v.get("license_plate") or "", v.get("serial_no") or "", familia, subfamilia,
            _m2o_name(v.get("cost_center_id")), _m2o_name(v.get("owner_id")).strip(), int(bool(v.get("subasta"))),
            v.get("state_date") or None)
