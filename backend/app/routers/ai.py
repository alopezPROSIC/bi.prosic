from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openai import OpenAIError

from ..ai.analyze import run_analysis
from ..ai.chat import chat_stream, with_heartbeat
from ..ai.client import AiDisabled, get_openai
from ..auth import User, require_ai, require_module
from ..cache import Cache
from ..config import Settings, runtime_settings
from ..deps import ai_rate_limit, get_cache, get_odoo
from ..odoo_client import OdooClient, OdooError
from ..schemas.ai import AiAnalysis, AnalyzeRequest, ChatRequest

log = logging.getLogger("prosic.ai")
router = APIRouter(prefix="/api/ai", tags=["ai"])


def _check_enabled(settings: Settings) -> None:
    if not settings.ai_enabled:
        raise HTTPException(503, "La IA está desactivada o sin configurar (panel de administración / .env)")


@router.post("/analyze", response_model=AiAnalysis)
async def analyze(body: AnalyzeRequest, user: User = Depends(ai_rate_limit), odoo: OdooClient = Depends(get_odoo),
                  cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> AiAnalysis:
    require_ai(user)
    require_module(user, body.modulo)
    _check_enabled(settings)
    try:
        return await run_analysis(odoo, cache, settings, user.username, body.modulo, user.scope_filters(body.filtros))
    except AiDisabled as exc:
        raise HTTPException(503, str(exc)) from exc
    except OdooError as exc:
        raise HTTPException(502, f"No fue posible consultar Odoo: {exc}") from exc
    except (OpenAIError, ValueError) as exc:
        log.error("Error de análisis IA: %s", exc)
        raise HTTPException(502, "El servicio de IA no pudo generar el análisis. Intenta de nuevo.") from exc


@router.post("/chat")
async def chat(body: ChatRequest, user: User = Depends(ai_rate_limit), odoo: OdooClient = Depends(get_odoo),
               cache: Cache = Depends(get_cache), settings: Settings = Depends(runtime_settings)) -> StreamingResponse:
    require_ai(user)
    _check_enabled(settings)
    if body.messages[-1].role != "user":
        raise HTTPException(422, "El último mensaje debe ser del usuario")
    try:
        get_openai()
    except AiDisabled as exc:
        raise HTTPException(503, str(exc)) from exc
    gen = chat_stream(body.messages, user.username, odoo=odoo, cache=cache, settings=settings, scope=user)
    return StreamingResponse(
        with_heartbeat(gen), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
