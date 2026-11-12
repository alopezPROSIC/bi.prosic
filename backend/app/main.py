"""PROSIC BI — API FastAPI."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .cache import Cache
from .config import get_settings
from .odoo_client import OdooClient, OdooForbiddenMethod
from .routers import admin, ai, auth, kpis, odoo
from .services import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app.state.odoo = OdooClient(settings)
    app.state.cache = Cache(settings.REDIS_URL)
    task = None
    if settings.BACKGROUND_TASKS:
        task = asyncio.create_task(scheduler.run(app.state.odoo, app.state.cache, settings))
    try:
        yield
    finally:
        if task:
            task.cancel()
        await app.state.odoo.aclose()
        await app.state.cache.aclose()


app = FastAPI(title="PROSIC BI", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json",
              redoc_url=None)

_settings = get_settings()
if _settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # Sitio privado: que ningún buscador lo indexe.
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
    return response


@app.exception_handler(OdooForbiddenMethod)
async def _forbidden(_: Request, exc: OdooForbiddenMethod) -> JSONResponse:
    logging.getLogger("prosic.odoo").error("Bloqueado: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Operación no permitida (cliente de solo lectura)"})


@app.get("/api/ping", include_in_schema=False)
async def ping() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(odoo.router)
app.include_router(kpis.router)
app.include_router(ai.router)
app.include_router(admin.router)
