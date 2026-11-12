"""Cliente OpenAI compartido + registro de tokens y costo estimado."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from ..config import get_settings

log = logging.getLogger("prosic.ai")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class AiDisabled(Exception):
    pass


@lru_cache
def get_openai() -> AsyncOpenAI:
    s = get_settings()
    if not s.ai_enabled:
        raise AiDisabled("Configura la clave y el modelo del proveedor de IA (AI_PROVIDER) para habilitarla")
    # El SDK ya implementa reintentos con backoff exponencial ante 429/5xx/timeouts (respeta retry-after).
    # Groq expone una API compatible con OpenAI: solo cambia base_url.
    return AsyncOpenAI(api_key=s.ai_api_key, base_url=s.ai_base_url, timeout=s.OPENAI_TIMEOUT_SECONDS,
                       max_retries=s.OPENAI_MAX_RETRIES)


def compact_json(data: Any) -> str:
    """JSON sin espacios: menos tokens (importa con los límites por minuto del plan gratuito de Groq)."""
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


@lru_cache
def system_prompt() -> str:
    return (PROMPTS_DIR / "analista.md").read_text(encoding="utf-8").strip()


def log_usage(kind: str, user: str, usage: Any, settings: Any = None) -> dict[str, Any]:
    """Registra tokens y costo estimado en USD. Devuelve un dict serializable."""
    s = settings or get_settings()
    if usage is None:
        return {}
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = prompt / 1e6 * s.OPENAI_PRICE_INPUT_PER_1M + completion / 1e6 * s.OPENAI_PRICE_OUTPUT_PER_1M
    data = {"model": s.ai_model, "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "costo_estimado_usd": round(cost, 6)}
    log.info("ia %s provider=%s user=%s model=%s prompt_tokens=%d completion_tokens=%d costo_usd=%.6f",
             kind, s.AI_PROVIDER, user, s.ai_model, prompt, completion, cost)
    return data


class UsageTotals:
    """Acumulador simple con la misma interfaz que ``usage`` del SDK (acepta objeto o dict)."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def add(self, usage: Any) -> None:
        if not usage:
            return
        get = usage.get if isinstance(usage, dict) else (lambda k, d=0: getattr(usage, k, d))
        self.prompt_tokens += int(get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(get("completion_tokens", 0) or 0)


def chunk_usage(chunk: Any) -> Any:
    """Uso de tokens de un fragmento de streaming: OpenAI lo pone en ``usage``; Groq en ``x_groq.usage``."""
    if getattr(chunk, "usage", None):
        return chunk.usage
    extra = getattr(chunk, "model_extra", None) or {}
    x_groq = extra.get("x_groq") or {}
    return x_groq.get("usage") if isinstance(x_groq, dict) else None
