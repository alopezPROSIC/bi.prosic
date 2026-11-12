"""POST /api/ai/analyze: KPIs agregados -> OpenAI (structured outputs) -> análisis."""
from __future__ import annotations

import hashlib
import json
import logging

from openai import BadRequestError

from ..cache import Cache, make_key
from ..config import Settings
from ..odoo_client import OdooClient
from ..schemas.ai import AiAnalysis
from ..schemas.kpis import KpiFilters, ModuleName
from ..services.registry import get_module_kpis
from .anonymizer import Anonymizer, ai_payload
from .client import compact_json, get_openai, log_usage, system_prompt

log = logging.getLogger("prosic.ai")

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumen", "hallazgos", "recomendaciones", "preguntas_sugeridas"],
    "properties": {
        "resumen": {"type": "string", "description": "3 a 5 líneas con lo más importante del periodo."},
        "hallazgos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["titulo", "detalle", "severidad", "kpi_relacionado"],
                "properties": {
                    "titulo": {"type": "string"},
                    "detalle": {"type": "string", "description": "Cuantificado: MXN, %, variación."},
                    "severidad": {"type": "string", "enum": ["info", "alerta", "critico"]},
                    "kpi_relacionado": {"type": "string", "description": "Clave del indicador o gráfica."},
                },
            },
        },
        "recomendaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["accion", "impacto_esperado", "prioridad"],
                "properties": {
                    "accion": {"type": "string"},
                    "impacto_esperado": {"type": "string"},
                    "prioridad": {"type": "string", "enum": ["alta", "media", "baja"]},
                },
            },
        },
        "preguntas_sugeridas": {"type": "array", "items": {"type": "string"}},
    },
}

MODULE_TITLES = {"ventas": "Ventas y facturación", "rentas": "Rentas e inventario de equipos",
                 "mantenimiento": "Mantenimiento", "compras": "Compras y proveedores",
                 "pronostico": "Pronóstico a 6 meses y calendario de vencimientos"}


async def run_analysis(odoo: OdooClient, cache: Cache, settings: Settings, user: str, module: ModuleName,
                       filters: KpiFilters) -> AiAnalysis:
    kpis = await get_module_kpis(odoo, cache, settings, module, filters)
    if not kpis.available:
        return AiAnalysis(resumen=f"El módulo no está disponible en Odoo: {kpis.reason}", hallazgos=[],
                          recomendaciones=[], preguntas_sugeridas=[], modulo=module)

    anon = Anonymizer(settings.AI_ANONYMIZE)
    payload = ai_payload(kpis, anon)
    payload_json = compact_json(payload)
    data_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    key = make_key("ai", module, filters.model_dump(mode="json"), data_hash, settings.AI_PROVIDER, settings.ai_model)
    hit = await cache.get_json(key)
    if hit:
        return AiAnalysis.model_validate({**hit, "cached": True})

    client = get_openai()
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": (
            f"Analiza los KPIs del módulo «{MODULE_TITLES[module]}». Devuelve un resumen de 3 a 5 líneas, "
            "de 3 a 6 hallazgos ordenados por importancia, de 2 a 5 recomendaciones y 3 preguntas de seguimiento "
            "que el usuario podría hacerle al asistente.\n\nDatos (JSON):\n" + payload_json)},
    ]
    try:
        # Salida estructurada estricta (OpenAI; en Groq: openai/gpt-oss-120b y gpt-oss-20b).
        resp = await client.chat.completions.create(
            model=settings.ai_model, messages=messages,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "analisis_kpis", "strict": True, "schema": ANALYSIS_SCHEMA}},
        )
    except BadRequestError as exc:
        # Modelos sin json_schema (p. ej. llama en Groq): modo JSON + esquema en el prompt + validación local.
        log.warning("El modelo %s no aceptó json_schema (%s); se usa modo JSON simple.", settings.ai_model, exc)
        messages[-1] = {"role": "user", "content": messages[-1]["content"] + (
            "\n\nResponde ÚNICAMENTE con un objeto JSON que cumpla este JSON Schema:\n" + compact_json(ANALYSIS_SCHEMA))}
        resp = await client.chat.completions.create(model=settings.ai_model, messages=messages,
                                                    response_format={"type": "json_object"})
    usage = log_usage("analyze", user, resp.usage, settings)
    msg = resp.choices[0].message
    if getattr(msg, "refusal", None):
        raise ValueError(f"El modelo rechazó la solicitud: {msg.refusal}")
    data = anon.deanonymize_obj(json.loads(msg.content or "{}"))
    result = AiAnalysis.model_validate({**data, "modulo": module, "model": settings.ai_model, "usage": usage})
    await cache.set_json(key, result.model_dump(mode="json"), settings.AI_CACHE_TTL_SECONDS)
    return result
