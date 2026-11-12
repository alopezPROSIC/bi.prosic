"""Modelos tipados de respuesta de KPIs (contrato con el frontend y con la IA)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Unit = Literal["MXN", "%", "count", "days"]
Entity = Literal["cliente", "proveedor", "vendedor"]
CompareMode = Literal["previous", "year", "none"]
ModuleName = Literal["ventas", "rentas", "mantenimiento", "compras", "pronostico"]


class KpiFilters(BaseModel):
    date_from: date
    date_to: date
    company_id: int | None = None
    warehouse_id: int | None = None
    branch_id: int | None = None
    compare: CompareMode = "previous"
    # Alcance del usuario (lo fija el backend, ver auth.User.scope_filters):
    # varios almacenes/sucursales permitidos cuando no se elige uno, y modo estricto: una sección que no puede
    # filtrarse por ese alcance se oculta en lugar de mostrar datos de toda la empresa.
    allowed_warehouse_ids: list[int] | None = None
    allowed_branch_ids: list[int] | None = None
    strict_scope: bool = False

    @model_validator(mode="after")
    def _check(self) -> "KpiFilters":
        if self.date_to < self.date_from:
            raise ValueError("date_to debe ser mayor o igual a date_from")
        if (self.date_to - self.date_from).days > 366 * 3:
            raise ValueError("El rango máximo es de 3 años")
        return self

    def previous(self) -> "KpiFilters | None":
        if self.compare == "none":
            return None
        if self.compare == "year":
            try:
                f, t = self.date_from.replace(year=self.date_from.year - 1), self.date_to.replace(year=self.date_to.year - 1)
            except ValueError:  # 29 de febrero
                f, t = self.date_from - timedelta(days=365), self.date_to - timedelta(days=365)
        else:
            span = (self.date_to - self.date_from).days + 1
            t = self.date_from - timedelta(days=1)
            f = t - timedelta(days=span - 1)
        return self.model_copy(update={"date_from": f, "date_to": t, "compare": "none"})


class Period(BaseModel):
    date_from: date
    date_to: date


class KpiCard(BaseModel):
    key: str
    label: str
    value: float | None
    previous: float | None = None
    delta_pct: float | None = None
    unit: Unit = "MXN"
    higher_is_better: bool = True
    hint: str | None = None


class ChartSeries(BaseModel):
    key: str
    label: str


class Chart(BaseModel):
    key: str
    title: str
    type: Literal["line", "bar", "hbar", "donut", "stacked_bar"]
    unit: Unit = "MXN"
    x_key: str = "label"
    series: list[ChartSeries]
    data: list[dict[str, Any]]
    # Si las etiquetas del eje X son nombres de terceros (para anonimizar).
    entity: Entity | None = None
    # Drill-down: al hacer clic en una barra se aplica este filtro global con el "_id" de la fila.
    drill: Literal["warehouse_id", "branch_id"] | None = None


class Column(BaseModel):
    key: str
    label: str
    type: Literal["text", "money", "number", "date", "percent", "days"] = "text"
    entity: Entity | None = None


class Table(BaseModel):
    key: str
    title: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    # False => es detalle línea por línea y NO se envía a la IA.
    ai_include: bool = True
    # Si se define, cada fila con "_id" enlaza al registro en Odoo (modelo indicado).
    odoo_model: str | None = None


class ModuleKpis(BaseModel):
    module: ModuleName
    available: bool = True
    reason: str | None = None
    filters: KpiFilters
    period: Period
    previous_period: Period | None = None
    cards: list[KpiCard] = Field(default_factory=list)
    charts: list[Chart] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    generated_at: datetime
