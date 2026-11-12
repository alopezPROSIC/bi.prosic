"""Configuración del servicio. Todo viene de variables de entorno (.env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # Odoo
    ODOO_URL: str
    ODOO_DB: str
    ODOO_USER: str
    ODOO_API_KEY: str
    ODOO_TZ: str = "America/Mexico_City"
    ODOO_TIMEOUT_SECONDS: float = 60
    ODOO_MAX_RETRIES: int = 3
    ODOO_MAX_CONCURRENCY: int = 4
    ODOO_MAX_RECORDS: int = 20000

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"
    KPI_CACHE_TTL_SECONDS: int = 300
    AI_CACHE_TTL_SECONDS: int = 21600

    # IA. Proveedor: "openai" o "groq" (API compatible con OpenAI, plan gratuito).
    AI_PROVIDER: Literal["openai", "groq"] = "openai"
    # Opcional: otra URL compatible con OpenAI (sobrescribe la del proveedor).
    AI_BASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = ""
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    OPENAI_TIMEOUT_SECONDS: float = 90
    OPENAI_MAX_RETRIES: int = 3
    OPENAI_PRICE_INPUT_PER_1M: float = 0.0
    OPENAI_PRICE_OUTPUT_PER_1M: float = 0.0
    AI_ANONYMIZE: bool = True
    AI_ENABLED: bool = True  # interruptor general (editable en el panel)
    AI_RATE_LIMIT_PER_MINUTE: int = 6
    AI_RATE_LIMIT_PER_DAY: int = 200

    # Auth
    JWT_SECRET: str = Field(min_length=16)
    JWT_EXPIRE_MINUTES: int = 480
    # Inicio de sesión con Google (solo cuentas del dominio). Client ID de un cliente OAuth «Aplicación web».
    GOOGLE_CLIENT_ID: str = ""
    ALLOWED_EMAIL_DOMAIN: str = "prosic.com"
    # Correos que entran como administradores aunque aún no existan en el panel (arranque inicial).
    ADMIN_EMAILS: str = ""
    # Usuarios nuevos del dominio: quedan «pendiente» hasta que un admin los apruebe (o se aprueban con este rol).
    NEW_USER_DEFAULT_ROLE: str = "consulta"
    NEW_USER_AUTO_APPROVE: bool = False
    # Login con usuario/contraseña (AUTH_USERS). Vacío = solo si no hay GOOGLE_CLIENT_ID (acceso de emergencia).
    ALLOW_PASSWORD_LOGIN: bool | None = None
    AUTH_USERS: str = ""

    # Historial diario de flota y estado del programador (SQLite). Vacío = desactivado.
    HISTORY_DB_PATH: str = "/data/prosic.db"
    # Precálculo en segundo plano de los KPIs del mes en curso (minutos; 0 = desactivado)
    PREFETCH_MINUTES: int = 5
    # Activa el programador (precálculo + resumen semanal). Se desactiva en pruebas.
    BACKGROUND_TASKS: bool = True

    # Resumen semanal por correo (opcional: requiere SMTP_HOST y SUMMARY_RECIPIENTS)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TLS: bool = True
    SUMMARY_RECIPIENTS: str = ""
    SUMMARY_WEEKDAY: int = 0  # 0 = lunes
    SUMMARY_HOUR: int = 8  # hora local (ODOO_TZ)
    # Alertas del resumen
    ALERT_UTILIZATION_MIN_PCT: float = 35
    ALERT_AR_OVERDUE_MAX_PCT: float = 50
    ALERT_QUARANTINE_MAX_PCT: float = 20

    # Web
    CORS_ORIGINS: str = ""
    LOG_LEVEL: str = "INFO"

    @field_validator("ODOO_URL")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def ai_api_key(self) -> str:
        return self.GROQ_API_KEY if self.AI_PROVIDER == "groq" else self.OPENAI_API_KEY

    @property
    def ai_model(self) -> str:
        return self.GROQ_MODEL if self.AI_PROVIDER == "groq" else self.OPENAI_MODEL

    @property
    def ai_base_url(self) -> str | None:
        if self.AI_BASE_URL:
            return self.AI_BASE_URL.rstrip("/")
        return GROQ_BASE_URL if self.AI_PROVIDER == "groq" else None

    @property
    def ai_enabled(self) -> bool:
        return bool(self.AI_ENABLED and self.ai_api_key and self.ai_model)

    @property
    def password_login(self) -> bool:
        return self.ALLOW_PASSWORD_LOGIN if self.ALLOW_PASSWORD_LOGIN is not None else not self.GOOGLE_CLIENT_ID

    @property
    def admin_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def runtime_settings() -> Settings:
    """Settings del .env con los parámetros editados en el panel de administración encima."""
    base = get_settings()
    from .store import get_store  # import diferido: evita ciclo

    overrides = get_store(base.HISTORY_DB_PATH).settings()
    return base.model_copy(update=overrides) if overrides else base
