from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .kpis import KpiFilters, ModuleName


class AnalyzeRequest(BaseModel):
    modulo: ModuleName
    filtros: KpiFilters


class Hallazgo(BaseModel):
    titulo: str
    detalle: str
    severidad: Literal["info", "alerta", "critico"]
    kpi_relacionado: str


class Recomendacion(BaseModel):
    accion: str
    impacto_esperado: str
    prioridad: Literal["alta", "media", "baja"]


class AiAnalysis(BaseModel):
    resumen: str
    hallazgos: list[Hallazgo]
    recomendaciones: list[Recomendacion]
    preguntas_sugeridas: list[str]
    # metadatos (no los genera el modelo)
    modulo: ModuleName | None = None
    cached: bool = False
    model: str | None = None
    usage: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=30)
