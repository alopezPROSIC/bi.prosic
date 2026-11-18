"""Protecciones para no saturar Odoo: tope por minuto, una sola consulta por vista y corrida nocturna sin ventana fría."""
import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.cache import Cache, make_key
from app.config import get_settings
from app.odoo_client import OdooClient, OdooError, RateLimiter
from app.schemas.kpis import KpiFilters
from app.services import registry, scheduler
from app.services.registry import get_module_kpis

MX = ZoneInfo("America/Mexico_City")


def settings(**kw):
    return get_settings().model_copy(update=kw)


# ------------------------------------------------------------------ tope por minuto
def test_limitador_deja_pasar_hasta_el_tope_sin_esperar():
    async def run():
        lim = RateLimiter(3, 5)
        for _ in range(3):
            await asyncio.wait_for(lim.acquire(), 0.5)

    asyncio.run(run())


def test_limitador_espera_su_turno_cuando_se_alcanza_el_tope(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        clock[0] += s

    clock = [1000.0]
    monkeypatch.setattr("app.odoo_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.odoo_client.asyncio.sleep", fake_sleep)

    async def run():
        lim = RateLimiter(2, 120)
        for _ in range(3):  # la 3.ª debe esperar a que venza la ventana de 60 s
            await lim.acquire()

    asyncio.run(run())
    assert len(slept) == 1 and 59 <= slept[0] <= 60


def test_limitador_falla_si_la_espera_supera_el_maximo():
    async def run():
        lim = RateLimiter(1, 5)  # la ventana es de 60 s > 5 s de espera máxima
        await lim.acquire()
        await lim.acquire()

    with pytest.raises(OdooError, match="tope"):
        asyncio.run(run())


def test_sin_tope_no_limita():
    async def run():
        lim = RateLimiter(0, 1)
        for _ in range(500):
            await lim.acquire()

    asyncio.run(run())


def test_el_tope_aplica_a_cada_llamada_http_y_no_pausa_a_odoo():
    """Superar el tope del BI no es una caída de Odoo: no debe activar la pausa de 10 min."""
    calls = []

    def handler(request):
        calls.append(1)
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 7})

    s = settings(ODOO_MAX_CALLS_PER_MINUTE=2, ODOO_RATE_MAX_WAIT_SECONDS=1)
    client = OdooClient(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def run():
        await client.version()
        await client.version()
        with pytest.raises(OdooError, match="tope"):
            await client.version()

    asyncio.run(run())
    assert len(calls) == 2  # la tercera nunca salió hacia Odoo
    assert client._paused_until == 0.0  # noqa: SLF001


# ------------------------------------------------------------------ una consulta por vista
def test_misma_vista_en_paralelo_consulta_a_odoo_una_sola_vez(monkeypatch):
    llamadas = []

    async def builder(ctx):
        llamadas.append(1)
        await asyncio.sleep(0.05)  # simula el tiempo de Odoo: las demás peticiones llegan mientras tanto
        return [], [], []

    monkeypatch.setitem(registry.BUILDERS, "ventas", builder)
    cache = Cache("")
    f = KpiFilters(date_from=date(2026, 11, 1), date_to=date(2026, 11, 17), compare="none")

    async def run():
        return await asyncio.gather(*[
            get_module_kpis(None, cache, get_settings(), "ventas", f) for _ in range(10)  # type: ignore[arg-type]
        ])

    res = asyncio.run(run())
    assert len(llamadas) == 1
    assert all(r.module == "ventas" for r in res)
    assert registry._inflight == {}  # noqa: SLF001  (sin fugas de candados)


def test_vistas_distintas_no_se_bloquean_entre_si(monkeypatch):
    llamadas = []

    async def builder(ctx):
        llamadas.append(ctx.filters.date_to)
        return [], [], []

    monkeypatch.setitem(registry.BUILDERS, "ventas", builder)
    cache = Cache("")

    async def run():
        for d in (15, 16, 17):
            f = KpiFilters(date_from=date(2026, 11, 1), date_to=date(2026, 11, d), compare="none")
            await get_module_kpis(None, cache, get_settings(), "ventas", f)  # type: ignore[arg-type]

    asyncio.run(run())
    assert len(llamadas) == 3


def test_si_el_calculo_falla_el_candado_se_libera_y_el_siguiente_reintenta(monkeypatch):
    intentos = []

    async def builder(ctx):
        intentos.append(1)
        if len(intentos) == 1:
            raise OdooError("Odoo caído")
        return [], [], []

    monkeypatch.setitem(registry.BUILDERS, "ventas", builder)
    cache = Cache("")
    f = KpiFilters(date_from=date(2026, 11, 1), date_to=date(2026, 11, 17), compare="none")

    async def run():
        with pytest.raises(OdooError):
            await get_module_kpis(None, cache, get_settings(), "ventas", f)  # type: ignore[arg-type]
        return await get_module_kpis(None, cache, get_settings(), "ventas", f)  # type: ignore[arg-type]

    asyncio.run(run())
    assert len(intentos) == 2 and registry._inflight == {}  # noqa: SLF001


# ------------------------------------------------------------------ corrida nocturna
def test_la_corrida_nocturna_no_vacia_el_cache_antes_de_precalcular(monkeypatch):
    cache = Cache("")
    vista_de_usuario = KpiFilters(date_from=date(2026, 11, 1), date_to=date(2026, 11, 17), compare="none")
    key = make_key("kpi", "ventas", vista_de_usuario.model_dump(mode="json"))
    visto_durante_precalculo = []

    async def fake_sync_fleet(odoo, s):
        return None

    async def fake_prefetch(odoo, c, s, day):
        visto_durante_precalculo.append(await c.get_json(key))

    monkeypatch.setattr(scheduler, "sync_fleet", fake_sync_fleet)
    monkeypatch.setattr(scheduler, "prefetch", fake_prefetch)

    async def run():
        await cache.set_json(key, {"cacheado": True}, 3600)
        await scheduler.nightly(None, cache, get_settings())  # type: ignore[arg-type]

    asyncio.run(run())
    assert visto_durante_precalculo == [{"cacheado": True}]  # la vista del usuario seguía en caché: sin ventana fría


def test_variantes_de_comparacion_a_precalcular():
    assert scheduler.prefetch_modes(settings(PREFETCH_COMPARE_MODES="previous")) == ["previous"]
    assert scheduler.prefetch_modes(settings(PREFETCH_COMPARE_MODES="previous, none")) == ["previous", "none"]
    assert scheduler.prefetch_modes(settings(PREFETCH_COMPARE_MODES="none,none,basura")) == ["none"]
    assert scheduler.prefetch_modes(settings(PREFETCH_COMPARE_MODES="")) == ["previous"]


def test_precalculo_consulta_solo_las_variantes_configuradas(monkeypatch):
    pedidos = []

    async def fake_get(odoo, cache, s, module, f, use_cache=True, ttl=None):
        pedidos.append((module, f.compare))

    monkeypatch.setattr(scheduler, "get_module_kpis", fake_get)
    asyncio.run(scheduler.prefetch(None, Cache(""), settings(PREFETCH_COMPARE_MODES="previous"), date(2026, 11, 18)))  # type: ignore[arg-type]
    assert {c for _, c in pedidos} == {"previous"} and len(pedidos) == len(scheduler.MODULES)
    pedidos.clear()
    asyncio.run(scheduler.prefetch(None, Cache(""), settings(PREFETCH_COMPARE_MODES="previous,none"), date(2026, 11, 18)))  # type: ignore[arg-type]
    assert len(pedidos) == 2 * len(scheduler.MODULES)
