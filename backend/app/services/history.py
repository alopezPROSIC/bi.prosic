"""Historial diario de la flota (SQLite).

Odoo solo sabe dónde está cada equipo HOY; para graficar la tendencia de utilización y mantenimiento
guardamos una foto diaria por almacén y estatus. La foto se toma cada vez que se calcula el módulo de
rentas sin filtros (lo hace el precálculo en segundo plano) y se sobrescribe durante el día.
``HISTORY_DB_PATH`` vacío desactiva el historial.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("prosic.history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_daily (
    day TEXT NOT NULL,
    warehouse_id INTEGER NOT NULL,
    warehouse TEXT NOT NULL,
    status TEXT NOT NULL,
    qty REAL NOT NULL,
    PRIMARY KEY (day, warehouse_id, status)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class History:
    def __init__(self, path: str):
        self.path = path
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self._conn() as c:
                c.executescript(_SCHEMA)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    # ---------- flota
    def _record(self, day: date, rows: list[tuple[int, str, str, float]]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM fleet_daily WHERE day = ?", (day.isoformat(),))
            c.executemany("INSERT INTO fleet_daily VALUES (?, ?, ?, ?, ?)",
                          [(day.isoformat(), wid, name, status, qty) for wid, name, status, qty in rows])

    def _series(self, d_from: date, d_to: date, warehouse_ids: list[int] | int | None) -> dict[str, dict[str, float]]:
        sql = "SELECT day, status, SUM(qty) FROM fleet_daily WHERE day BETWEEN ? AND ?"
        args: list = [d_from.isoformat(), d_to.isoformat()]
        if isinstance(warehouse_ids, int):
            warehouse_ids = [warehouse_ids]
        if warehouse_ids:
            sql += f" AND warehouse_id IN ({','.join('?' * len(warehouse_ids))})"
            args += list(warehouse_ids)
        out: dict[str, dict[str, float]] = {}
        with self._conn() as c:
            for day, status, qty in c.execute(sql + " GROUP BY day, status ORDER BY day", args):
                out.setdefault(day, {})[status] = qty
        return out

    async def record_fleet(self, day: date, rows: list[tuple[int, str, str, float]]) -> None:
        if self.enabled:
            try:
                await asyncio.to_thread(self._record, day, rows)
            except sqlite3.Error as exc:
                log.warning("No se pudo guardar el historial de flota: %s", exc)

    async def fleet_series(self, d_from: date, d_to: date,
                           warehouse_ids: list[int] | int | None) -> dict[str, dict[str, float]]:
        if not self.enabled:
            return {}
        try:
            return await asyncio.to_thread(self._series, d_from, d_to, warehouse_ids)
        except sqlite3.Error as exc:
            log.warning("No se pudo leer el historial de flota: %s", exc)
            return {}

    # ---------- llave/valor (p. ej. última semana en que se envió el resumen)
    def _get(self, k: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
            return row[0] if row else None

    def _set(self, k: str, v: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    async def get(self, k: str) -> str | None:
        return await asyncio.to_thread(self._get, k) if self.enabled else None

    async def set(self, k: str, v: str) -> None:
        if self.enabled:
            await asyncio.to_thread(self._set, k, v)


@lru_cache(maxsize=4)
def get_history(path: str) -> History:
    try:
        return History(path)
    except (OSError, sqlite3.Error) as exc:
        log.warning("Historial desactivado (%s): %s", path, exc)
        return History("")
