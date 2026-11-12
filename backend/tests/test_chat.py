"""Flujo completo del chat con un cliente OpenAI simulado (sin red)."""
import asyncio
import json
from types import SimpleNamespace as NS

from fake_odoo import FakeOdoo
from sample_data import RELATIONS, TYPES, base_data

import app.ai.chat as chat_mod
from app.cache import Cache
from app.config import get_settings
from app.schemas.ai import ChatMessage


def chunk(content=None, tool_calls=None, usage=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))], usage=usage)


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class FakeCompletions:
    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        if len(self.requests) == 1:
            args = '{"fecha_corte": null, "sucursal": null, "compania": null}'
            tc = NS(index=0, id="call_1", function=NS(name="get_cxc", arguments=args))
            return FakeStream([chunk(tool_calls=[tc]), NS(choices=[], usage=NS(prompt_tokens=100, completion_tokens=10))])
        return FakeStream([chunk("El mayor saldo vencido es de "), chunk("CLIEN"), chunk("TE-A con $1,160."),
                           NS(choices=[], usage=NS(prompt_tokens=200, completion_tokens=20))])


def test_chat_stream_tool_call_and_deanonymization(monkeypatch):
    completions = FakeCompletions()
    monkeypatch.setattr(chat_mod, "get_openai", lambda: NS(chat=NS(completions=completions)))
    settings = get_settings().model_copy(update={"OPENAI_MODEL": "modelo-prueba", "AI_ANONYMIZE": True})

    async def collect():
        out = []
        async for ev in chat_mod.chat_stream([ChatMessage(role="user", content="¿Quién debe más?")], "ana",
                                             odoo=FakeOdoo(base_data(), TYPES, RELATIONS),
                                             cache=Cache(""), settings=settings):
            out.append(ev)
        return out

    events = asyncio.run(collect())
    kinds = [e.split("\n")[0].removeprefix("event: ") for e in events]
    assert kinds[0] == "tool_start" and "tool_result" in kinds and kinds[-1] == "done"
    text = "".join(json.loads(e.split("data: ", 1)[1])["text"] for e in events if e.startswith("event: token"))
    assert text == "El mayor saldo vencido es de Constructora Alfa SA con $1,160."
    # Lo que viaja a OpenAI no contiene el nombre real
    sent = json.dumps(completions.requests, ensure_ascii=False)
    assert "Constructora Alfa SA" not in sent
    assert "CLIENTE-A" in sent
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["usage"]["prompt_tokens"] == 300


class GroqCompletions:
    """Simula Groq: el uso llega en x_groq.usage (model_extra) y no se acepta stream_options."""

    def __init__(self):
        self.kwargs = []

    async def create(self, **kwargs):
        assert "stream_options" not in kwargs
        self.kwargs.append(kwargs)
        return FakeStream([chunk("Hola "), chunk("mundo."),
                           NS(choices=[], usage=None,
                              model_extra={"x_groq": {"usage": {"prompt_tokens": 50, "completion_tokens": 5}}})])


def test_chat_with_groq_provider(monkeypatch):
    completions = GroqCompletions()
    monkeypatch.setattr(chat_mod, "get_openai", lambda: NS(chat=NS(completions=completions)))
    settings = get_settings().model_copy(update={"AI_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x",
                                                 "GROQ_MODEL": "openai/gpt-oss-120b"})
    assert settings.ai_base_url == "https://api.groq.com/openai/v1" and settings.ai_enabled

    async def collect():
        return [ev async for ev in chat_mod.chat_stream([ChatMessage(role="user", content="hola")], "ana",
                                                        odoo=FakeOdoo(base_data(), TYPES, RELATIONS),
                                                        cache=Cache(""), settings=settings)]

    events = asyncio.run(collect())
    sent = completions.kwargs[0]
    assert sent["model"] == "openai/gpt-oss-120b"
    assert all("strict" not in t["function"] for t in sent["tools"])
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["usage"]["prompt_tokens"] == 50 and done["usage"]["model"] == "openai/gpt-oss-120b"
