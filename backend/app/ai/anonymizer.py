"""Privacidad: arma el payload AGREGADO que se envía a OpenAI y anonimiza
nombres de terceros (CLIENTE-A, PROVEEDOR-B, VENDEDOR-C…). El backend
des-anonimiza la respuesta antes de devolverla al usuario.

Nunca se envían RFC, correos, teléfonos, direcciones ni detalle línea por línea:
los KPIs solo contienen totales, series, top N y tablas agregadas; las tablas
marcadas ``ai_include=False`` (p. ej. rentas por vencer) se excluyen.
"""
from __future__ import annotations

import re
from typing import Any

from ..schemas.kpis import ModuleKpis

PREFIX = {"cliente": "CLIENTE", "proveedor": "PROVEEDOR", "vendedor": "VENDEDOR"}
ALIAS_RE = re.compile(r"\b(?:CLIENTE|PROVEEDOR|VENDEDOR)-[A-Z]{1,3}\b")
_NOT_ANON = {"Sin asignar", "Otros", "Sin categoría", ""}


def _code(n: int) -> str:
    """0->A, 25->Z, 26->AA …"""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Anonymizer:
    def __init__(self, enabled: bool, state: dict[str, Any] | None = None):
        self.enabled = enabled
        state = state or {}
        self.alias_to_real: dict[str, str] = dict(state.get("alias_to_real", {}))
        self.real_to_alias: dict[str, str] = {}
        for alias, real in self.alias_to_real.items():
            self.real_to_alias[f"{alias.split('-')[0]}|{real}"] = alias
        self.counters: dict[str, int] = dict(state.get("counters", {}))

    def state(self) -> dict[str, Any]:
        return {"alias_to_real": self.alias_to_real, "counters": self.counters}

    def alias(self, entity: str | None, name: Any) -> Any:
        if not self.enabled or not entity or not isinstance(name, str) or name in _NOT_ANON:
            return name
        prefix = PREFIX.get(entity, "TERCERO")
        key = f"{prefix}|{name}"
        if key not in self.real_to_alias:
            n = self.counters.get(prefix, 0)
            self.counters[prefix] = n + 1
            alias = f"{prefix}-{_code(n)}"
            self.real_to_alias[key] = alias
            self.alias_to_real[alias] = name
        return self.real_to_alias[key]

    def anonymize_text(self, text: str) -> str:
        """Reemplaza nombres reales conocidos (p. ej. en el historial del chat) por su alias."""
        if not self.enabled or not self.alias_to_real:
            return text
        for alias, real in sorted(self.alias_to_real.items(), key=lambda kv: -len(kv[1])):
            if len(real) >= 3:
                text = re.sub(re.escape(real), alias, text, flags=re.IGNORECASE)
        return text

    def deanonymize(self, text: str) -> str:
        if not self.alias_to_real:
            return text
        return ALIAS_RE.sub(lambda m: self.alias_to_real.get(m.group(0), m.group(0)), text)

    def deanonymize_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.deanonymize(obj)
        if isinstance(obj, list):
            return [self.deanonymize_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.deanonymize_obj(v) for k, v in obj.items()}
        return obj


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Quita campos internos ("_id" para enlaces/drill-down): los IDs de Odoo no viajan a la IA."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def ai_payload(kpis: ModuleKpis, anon: Anonymizer, max_points: int = 24) -> dict[str, Any]:
    """Resumen agregado y anonimizado de un módulo, listo para el modelo."""
    out: dict[str, Any] = {
        "modulo": kpis.module,
        "disponible": kpis.available,
        "periodo": {"desde": kpis.period.date_from.isoformat(), "hasta": kpis.period.date_to.isoformat()},
        "moneda": "MXN",
    }
    if kpis.previous_period:
        out["periodo_comparacion"] = {"desde": kpis.previous_period.date_from.isoformat(),
                                      "hasta": kpis.previous_period.date_to.isoformat()}
    if not kpis.available:
        out["motivo_no_disponible"] = kpis.reason
        return out
    out["indicadores"] = [
        {"kpi": c.key, "nombre": c.label, "valor": c.value, "anterior": c.previous, "variacion_pct": c.delta_pct,
         # diferencia ya calculada: evita que el modelo confunda el valor con la variación al redactar
         **({"diferencia": round(c.value - c.previous, 2)} if c.value is not None and c.previous is not None else {}),
         "unidad": c.unit, "mayor_es_mejor": c.higher_is_better, **({"nota": c.hint} if c.hint else {})}
        for c in kpis.cards
    ]
    out["graficas"] = [
        {"clave": ch.key, "titulo": ch.title, "unidad": ch.unit,
         "series": {s.key: s.label for s in ch.series},
         "datos": [{**_public(row), ch.x_key: anon.alias(ch.entity, row.get(ch.x_key))} for row in ch.data[:max_points]]}
        for ch in kpis.charts
    ]
    out["tablas"] = []
    for t in kpis.tables:
        if not t.ai_include:
            out.setdefault("omitido_por_privacidad", []).append(t.title)
            continue
        ent_cols = {c.key: c.entity for c in t.columns if c.entity}
        out["tablas"].append({
            "clave": t.key, "titulo": t.title,
            "columnas": {c.key: c.label for c in t.columns},
            "filas": [{k: anon.alias(ent_cols.get(k), v) for k, v in _public(row).items()} for row in t.rows[:max_points]],
        })
    if kpis.notes:
        out["notas_de_datos"] = kpis.notes
    return out
