"""OdooClient simulado en memoria para pruebas.

Implementa la misma interfaz pública de lectura (search_read, read_group,
search_count, fields_get, missing_fields) sobre listas de dicts, con un
evaluador de dominios suficiente para las pruebas: =, !=, in, not in, <, <=,
>, >=, child_of (por igualdad), operadores '|' y '&', y rutas con punto
resueltas contra otras tablas de la base simulada.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.odoo_client import ALLOWED_METHODS, OdooForbiddenMethod, OdooModelUnavailable

EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]


class FakeOdoo:
    def __init__(self, data: dict[str, list[dict[str, Any]]], types: dict[str, dict[str, str]] | None = None,
                 relations: dict[tuple[str, str], str] | None = None):
        self.data = data
        self.types = types or {}
        # (modelo, campo many2one) -> modelo relacionado, para rutas con punto
        self.relations = relations or {}
        self.calls: list[tuple[str, str]] = []

    # --- interfaz pública ---
    async def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        if method not in ALLOWED_METHODS:
            raise OdooForbiddenMethod(method)
        raise NotImplementedError

    async def fields_get(self, model: str) -> dict[str, dict[str, Any]]:
        self._check(model)
        names: set[str] = set(self.types.get(model, {}))
        for r in self.data[model]:
            names |= set(r)
        return {n: {"type": self.types.get(model, {}).get(n, "char")} for n in names | {"id"}}

    async def missing_fields(self, model: str, fields: list[str]) -> list[str]:
        available = await self.fields_get(model)
        return [f for f in fields if f.split(".")[0] not in available]

    async def search_read(self, model: str, domain: list, fields: list[str], order: str | None = None,
                          limit: int | None = None) -> list[dict[str, Any]]:
        self.calls.append((model, "search_read"))
        rows = [r for r in self._rows(model) if self._match(model, r, domain)]
        if order:
            key = order.split(",")[0].split()[0]
            rows.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse="desc" in order)
        rows = rows[:limit] if limit else rows
        return [{k: r.get(k, False) for k in ["id", *fields]} for r in rows]

    async def search_count(self, model: str, domain: list) -> int:
        self.calls.append((model, "search_count"))
        return sum(1 for r in self._rows(model) if self._match(model, r, domain))

    async def read_group(self, model: str, domain: list, fields: list[str], groupby: list[str] | str,
                         orderby: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        self.calls.append((model, "read_group"))
        gbs = [groupby] if isinstance(groupby, str) else list(groupby)
        rows = [r for r in self._rows(model) if self._match(model, r, domain)]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            groups[tuple(self._gkey(r, g) for g in gbs)].append(r)
        if not gbs:
            groups = {(): rows}
        out = []
        for key, members in groups.items():
            g: dict[str, Any] = {"__count": len(members), "__domain": []}
            for gb, k in zip(gbs, key):
                g[gb] = list(k) if isinstance(k, tuple) else k
            for spec in fields:
                if spec == "__count":
                    continue
                name, _, agg = spec.partition(":")
                if agg in ("max", "min"):
                    vals = [m.get(name) for m in members if m.get(name)]
                    g[name] = (max if agg == "max" else min)(vals) if vals else False
                else:
                    g[name] = sum(float(m.get(name) or 0) for m in members)
            out.append(g)
        return out

    # --- internos ---
    def _check(self, model: str) -> None:
        if model not in self.data:
            raise OdooModelUnavailable(model, "El modelo no existe en esta instancia (¿módulo no instalado?)")

    def _rows(self, model: str) -> list[dict[str, Any]]:
        self._check(model)
        return self.data[model]

    def _gkey(self, r: dict[str, Any], gb: str) -> Any:
        if ":" in gb:
            name, _ = gb.split(":")
            v = r.get(name)
            if not v:
                return False
            y, m = int(str(v)[:4]), int(str(v)[5:7])
            return f"{EN_MONTHS[m - 1]} {y}"
        v = r.get(gb, False)
        return tuple(v) if isinstance(v, (list, tuple)) else v

    def _value(self, model: str, r: dict[str, Any], path: str) -> Any:
        head, _, rest = path.partition(".")
        v = r.get(head, False)
        if not rest:
            return v
        rel = self.relations.get((model, head))
        if not rel or not v:
            return False
        rid = v[0] if isinstance(v, (list, tuple)) else v
        target = next((x for x in self.data.get(rel, []) if x["id"] == rid), None)
        return self._value(rel, target, rest) if target else False

    def _leaf(self, model: str, r: dict[str, Any], leaf: tuple) -> bool:
        field, op, val = leaf
        v = self._value(model, r, field)
        if isinstance(v, (list, tuple)) and v and op not in ("in", "not in"):
            v = v[0]
        if isinstance(v, (list, tuple)) and v:
            v = v[0]
        if op in ("=", "child_of"):
            return v == val or (val is False and v in (None, False, ""))
        if op == "!=":
            return not (v == val or (val is False and v in (None, False, "")))
        if op == "in":
            return v in val
        if op == "not in":
            return v not in val
        if v in (None, False):
            return False
        a, b = (str(v), str(val)) if isinstance(val, str) else (v, val)
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def _match(self, model: str, r: dict[str, Any], domain: list) -> bool:
        def ev(i: int) -> tuple[bool, int]:
            t = domain[i]
            if t == "|":
                a, j = ev(i + 1)
                b, k = ev(j)
                return a or b, k
            if t == "&":
                a, j = ev(i + 1)
                b, k = ev(j)
                return a and b, k
            return self._leaf(model, r, tuple(t)), i + 1

        i, ok = 0, True
        while i < len(domain):
            res, i = ev(i)
            ok = ok and res
        return ok
