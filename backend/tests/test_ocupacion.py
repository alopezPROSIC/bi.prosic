"""Tacómetro de ocupación diaria: cierre de la noche, corte de la mañana, lectura del momento y permisos."""
import asyncio
from datetime import datetime, timedelta

from fake_odoo import FakeOdoo
from fastapi.testclient import TestClient
from test_historico import TODAY, TZ, synced  # noqa: F401 — fixture

from app.cache import Cache
from app.config import runtime_settings
from app.deps import get_odoo
from app.main import app
from app.services import occupancy


def build(settings, odoo, cache=None, wh=None):
    return asyncio.run(occupancy.build(odoo, cache or Cache(""), settings, wh))


def test_close_and_live_reading(synced):  # noqa: F811
    settings, odoo, _ = synced
    # flota de renta: V1 (Monterrey) y V2 (Saltillo), ambos rentados al cierre; V3 no es de renta y V4 está archivado
    first = build(settings, odoo)
    assert (first["cierre"]["flota"], first["cierre"]["ocupadas"], first["cierre"]["pct"]) == (2, 2, 100)
    assert first["ahora"]["pct"] == 100 and sorted(first["estados_ocupados"]) == ["Entrega", "Rentado"]
    # V1 se devuelve hoy: «ahora» lo refleja (lectura nueva de Odoo), el cierre de anoche no cambia
    odoo.data["fleet.vehicle"][0]["state_id"] = [3, "Available"]
    now = build(settings, odoo)
    assert now["ahora"]["pct"] == 50 and now["cierre"]["pct"] == 100
    assert {a["nombre"]: a["pct"] for a in now["ahora"]["almacenes"]} == {"Monterrey": 0, "Saltillo": 100}
    assert len(now["tendencia"]) >= 5 and now["tendencia"][-1]["dia"] == now["cierre"]["dia"]


def test_live_reading_is_shared_and_cached(synced):  # noqa: F811
    settings, odoo, _ = synced
    calls = {"n": 0}

    class Counting(FakeOdoo):
        async def search_read(self, model, *a, **kw):
            calls["n"] += model == "fleet.vehicle"
            return await super().search_read(model, *a, **kw)

    c, cache = Counting(odoo.data), Cache("")
    build(settings, c, cache)
    build(settings, c, cache, wh=[1])
    build(settings, c, cache, wh=[2])
    assert calls["n"] == 1  # una sola consulta a Odoo para todos los filtros mientras dura el caché


def test_odoo_down_uses_last_reading(synced):  # noqa: F811
    settings, odoo, _ = synced
    build(settings, odoo)  # deja guardada una lectura

    class Down(FakeOdoo):
        async def search_read(self, *a, **kw):
            raise RuntimeError("Odoo caído")

    out = build(settings, Down(odoo.data))
    assert out["ahora"]["pct"] == 100 and out["ahora"]["stale"] is True
    assert out["cierre"]["pct"] == 100  # el cierre sale del histórico local


def test_morning_reading_once_a_day(synced):  # noqa: F811
    settings, odoo, _ = synced
    settings = settings.model_copy(update={"OCCUPANCY_MORNING_TIME": "08:00"})
    early = datetime.combine(TODAY, datetime.min.time(), TZ).replace(hour=7, minute=59)
    assert not asyncio.run(occupancy.take_morning_reading(odoo, Cache(""), settings, early))
    eight = early + timedelta(minutes=1)
    assert asyncio.run(occupancy.take_morning_reading(odoo, Cache(""), settings, eight))
    assert not asyncio.run(occupancy.take_morning_reading(odoo, Cache(""), settings, eight + timedelta(minutes=5)))
    out = build(settings, odoo)
    assert out["manana"]["pct"] == 100 and out["manana"]["ts"].startswith(f"{TODAY.isoformat()}T08:00")
    noon = early.replace(hour=12, minute=30) + timedelta(days=1)
    assert not asyncio.run(occupancy.take_morning_reading(odoo, Cache(""), settings, noon))  # ya no es «de la mañana»


def test_warehouse_filter(synced):  # noqa: F811
    settings, odoo, _ = synced
    out = build(settings, odoo, wh=[2])
    assert out["cierre"]["flota"] == 1 and [a["nombre"] for a in out["ahora"]["almacenes"]] == ["Saltillo"]


def test_goal_and_excluded_states_follow_settings(synced):  # noqa: F811
    settings, odoo, _ = synced
    odoo.data["fleet.vehicle"][0]["state_id"] = [3, "Available"]
    s = settings.model_copy(update={"OCCUPANCY_GOAL_PCT": 70, "FLEET_EXCLUDED_STATES": "Available"})
    out = build(s, odoo)
    assert out["meta"] == 70 and out["ahora"]["flota"] == 1 and out["ahora"]["pct"] == 100


def test_api_permissions_and_scope(synced):  # noqa: F811
    settings, odoo, _ = synced
    app.dependency_overrides[get_odoo] = lambda: odoo
    app.dependency_overrides[runtime_settings] = lambda: settings
    try:
        with TestClient(app) as c:
            def h(u, p):
                return {"Authorization": f"Bearer {c.post('/api/auth/login', json={'username': u, 'password': p}).json()['access_token']}"}
            assert c.get("/api/ocupacion").status_code == 401
            r = c.get("/api/ocupacion", headers=h("admin", "admin123"))
            assert r.status_code == 200 and r.json()["cierre"]["flota"] == 2
            # el gerente solo ve su almacén (Saltillo), aunque pida otro
            r = c.get("/api/ocupacion", headers=h("gerente", "ger123"), params={"warehouse_id": 1})
            assert r.status_code == 200 and r.json()["cierre"]["flota"] == 1
            assert [a["nombre"] for a in r.json()["ahora"]["almacenes"]] == ["Saltillo"]
    finally:
        app.dependency_overrides.pop(runtime_settings, None)
        app.dependency_overrides.pop(get_odoo, None)
