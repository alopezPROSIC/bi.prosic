"""POST /api/ai/chat: chat libre con function calling y respuesta en streaming (SSE).

Eventos SSE emitidos (``data:`` es JSON):
- ``token``        {"text": "..."}               fragmento de respuesta (ya des-anonimizado)
- ``tool_start``   {"name": "...", "args": {...}} el modelo pidió una herramienta
- ``tool_result``  {"name": "...", "data": ModuleKpis|null, "error": str|null}
- ``error``        {"message": "..."}
- ``done``         {"usage": {...}}
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any, AsyncIterator

from openai import OpenAIError, RateLimitError

from ..cache import Cache
from ..config import Settings
from ..odoo_client import OdooClient, OdooError
from ..schemas.ai import ChatMessage
from .anonymizer import Anonymizer
from .client import UsageTotals, chunk_usage, compact_json, get_openai, log_usage, system_prompt
from .tools import TOOL_MODULE, TOOLS, run_tool, tools_without_strict

log = logging.getLogger("prosic.ai")
MAX_TOOL_ROUNDS = 5
ANON_TTL = 86400

CHAT_RULES = """
Modo asistente (chat):
- Fecha de hoy: {today}. Zona horaria: {tz}.
- Tienes acceso DIRECTO a los datos de Odoo de PROSIC mediante las herramientas. NUNCA le pidas al usuario reportes,
  archivos ni datos que puedas consultar: llama primero las herramientas necesarias (puedes llamar varias) y responde
  con las cifras obtenidas. Solo pregunta si la solicitud es ambigua y no hay un valor razonable por defecto.
- Para cualquier cifra usa las herramientas disponibles; nunca respondas números de memoria.
- Pronósticos, proyecciones, flujo esperado o planeación: llama get_pronostico (y si hace falta contexto, get_cxc o
  get_utilizacion_rentas). Explica el método y el rango mín–máx que devuelve; no inventes otro modelo ni otras cifras.
- Ocupación histórica, promedios de un mes cerrado, estados de la flota en fechas pasadas o comparativos de ocupación
  entre periodos/almacenes: llama get_historico_flota (la foto de hoy y los ingresos están en get_utilizacion_rentas).
- En los indicadores, «valor» es la cifra del periodo (o el pronóstico), «anterior» la de comparación y «diferencia»
  = valor − anterior. Para hablar de aumentos o caídas en pesos usa «diferencia», nunca «valor». Lee también la «nota»
  de cada indicador (p. ej. saldos ya vencidos) antes de concluir.
- No hagas sumas ni cálculos propios: usa los totales que ya vienen en los datos (p. ej. la fila «Total 6 meses» del
  pronóstico). Si un dato no viene, dilo en lugar de estimarlo.
- Si el usuario no indica periodo, usa el mes en curso y dilo explícitamente. "Este trimestre" = trimestre calendario en curso.
- Para comparar sucursales o almacenes, consulta sin filtro y usa los desgloses por almacén/sucursal.
- Responde breve: 1 párrafo + viñetas con cifras. Usa formato Markdown simple.
"""


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _split_safe(buf: str) -> tuple[str, str]:
    """Emite hasta el último espacio para no partir un alias (CLIENTE-A) entre fragmentos."""
    idx = max(buf.rfind(" "), buf.rfind("\n"))
    if idx >= 0:
        return buf[: idx + 1], buf[idx + 1:]
    if len(buf) > 64:
        return buf, ""
    return "", buf


async def chat_stream(history: list[ChatMessage], user: str, *, odoo: OdooClient, cache: Cache,
                      settings: Settings, scope: Any = None) -> AsyncIterator[str]:
    anon_key = f"prosic:anon:{user}"
    anon = Anonymizer(settings.AI_ANONYMIZE, await cache.get_json(anon_key))
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt() + "\n" +
                                   CHAT_RULES.format(today=date.today().isoformat(), tz=settings.ODOO_TZ)}]
    msgs += [{"role": m.role, "content": anon.anonymize_text(m.content)} for m in history]
    usage = UsageTotals()
    client = get_openai()
    is_openai = settings.AI_PROVIDER == "openai" and not settings.AI_BASE_URL
    tools = TOOLS if is_openai else tools_without_strict()
    if scope is not None:  # solo las herramientas de los módulos que el rol puede ver
        tools = [t for t in tools if scope.can(TOOL_MODULE[t["function"]["name"]])]

    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            allow_tools = round_no < MAX_TOOL_ROUNDS
            kwargs: dict[str, Any] = {"model": settings.ai_model, "messages": msgs, "stream": True}
            if is_openai:
                kwargs["stream_options"] = {"include_usage": True}  # Groq reporta el uso en x_groq
            if allow_tools and tools:
                kwargs["tools"] = tools
            stream = await client.chat.completions.create(**kwargs)
            pending, full_text = "", ""
            calls: dict[int, dict[str, str]] = {}
            async for chunk in stream:
                usage.add(chunk_usage(chunk))
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    full_text += delta.content
                    emit, pending = _split_safe(pending + delta.content)
                    if emit:
                        yield sse("token", {"text": anon.deanonymize(emit)})
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
            if pending:
                yield sse("token", {"text": anon.deanonymize(pending)})
            if not calls:
                break

            ordered = [calls[i] for i in sorted(calls)]
            msgs.append({"role": "assistant", "content": full_text or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in ordered]})
            for c in ordered:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield sse("tool_start", {"name": c["name"], "args": anon.deanonymize_obj(args)})
                kpis = None
                try:
                    payload, kpis = await run_tool(c["name"], args, odoo=odoo, cache=cache, settings=settings, anon=anon,
                                                   scope=scope)
                except OdooError as exc:
                    log.warning("Herramienta %s falló: %s", c["name"], exc)
                    payload = {"error": "No fue posible consultar Odoo en este momento."}
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": compact_json(payload)})
                yield sse("tool_result", {"name": c["name"], "data": kpis.model_dump(mode="json") if kpis else None,
                                          "error": payload.get("error")})
    except RateLimitError as exc:
        log.error("IA chat: límite de uso alcanzado: %s", exc)
        yield sse("error", {"message": "Se alcanzó el límite de uso del proveedor de IA. Espera un minuto e intenta de nuevo."})
    except OpenAIError as exc:
        log.error("IA chat error: %s", exc)
        yield sse("error", {"message": "El servicio de IA no respondió. Intenta de nuevo en unos momentos."})
    finally:
        await cache.set_json(anon_key, anon.state(), ANON_TTL)

    yield sse("done", {"usage": log_usage("chat", user, usage, settings)})


async def with_heartbeat(gen: AsyncIterator[str], interval: float = 15.0) -> AsyncIterator[str]:
    """Intercala comentarios SSE para que Cloudflare/nginx no corten la conexión mientras Odoo responde."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in gen:
                await queue.put(item)
        except Exception as exc:  # noqa: BLE001
            log.exception("Error en el stream del chat")
            await queue.put(sse("error", {"message": f"Error interno: {type(exc).__name__}"}))
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield item
    finally:
        task.cancel()
