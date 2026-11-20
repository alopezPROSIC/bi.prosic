"""Comisiones — Fases 02 a 05: plan, cálculo por vendedor, comparativo con Finanzas y estado de cuenta."""
import asyncio
from datetime import date

import pytest
from fake_odoo import FakeOdoo
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_comisiones import RELATIONS, TODAY, build_data

from app.cache import Cache
from app.config import get_settings
from app.deps import get_odoo
from app.main import app
from app.schemas.kpis import KpiFilters
from app.services.commission_plan import (Ajuste, MesDatos, Ocupacion, Plan, Puesto, Sucursal, Tramo, Vendedor,
                                          calculate, get_plan_store, tramo)
from app.services.registry import get_module_kpis

MONTH = "2026-10"


def plan(**kw) -> Plan:
    base = dict(
        puestos=[Puesto(key="ejecutivo", label="Ejecutivo", tramos=[Tramo(desde=0, valor=0), Tramo(desde=80, valor=1),
                                                                     Tramo(desde=100, valor=2)]),
                 Puesto(key="gerente", label="Gerente", base="sucursal", tramos=[Tramo(desde=0, valor=0.5)])],
        vendedores=[Vendedor(odoo_id=7, nombre="Ana Ventas", puesto="ejecutivo", sucursal_id=1, correo="ana@prosic.com"),
                    Vendedor(odoo_id=8, nombre="Beto Ventas", puesto="ejecutivo", sucursal_id=2),
                    Vendedor(odoo_id=9, nombre="Carla Gerente", puesto="gerente", sucursal_id=1, correo="carla@prosic.com")],
        sucursales=[Sucursal(branch_id=1, nombre="01 Monterrey", almacenes=[10]),
                    Sucursal(branch_id=2, nombre="02 Tepotzotlán")])
    base.update(kw)
    return Plan(**base)


MES = MesDatos(metas_vendedor={7: 1600, 8: 1000}, metas_sucursal={1: 2000}, manual={7: 30},
               ajustes=[Ajuste(odoo_id=8, monto=5, motivo="Bono por cliente nuevo")])


def run(**filters):
    odoo = FakeOdoo(build_data(), relations=RELATIONS)
    f = KpiFilters(date_from=date(2026, 10, 1), date_to=TODAY, compare="none", **filters)
    return asyncio.run(get_module_kpis(odoo, Cache(""), get_settings(), "comisiones", f, use_cache=False))


def calc_rows(k):
    return {r["vendedor"]: r for r in next(t for t in k.tables if t.key == "calculo_comisiones").rows}


def configure(p: Plan | None = None, m: MesDatos | None = None):
    store = get_plan_store("")
    store.save_plan(p or plan(), "test")
    store.save_month(MONTH, m or MES, "test")


def test_tramo_picks_highest_reached():
    ts = [Tramo(desde=0, valor=0), Tramo(desde=80, valor=1), Tramo(desde=100, valor=2)]
    assert tramo(ts, 79.9).valor == 0 and tramo(ts, 80).valor == 1 and tramo(ts, 150).valor == 2
    assert tramo([Tramo(desde=50, valor=1)], 10) is None


def test_commission_per_seller():
    configure()
    rows = calc_rows(run())
    ana, beto, carla = rows["Ana Ventas"], rows["Beto Ventas"], rows["Carla Gerente"]
    # Ana: base 1600, meta 1600 → 100 % → 2 % = 32; Finanzas calculó 30 → diferencia 2
    assert (ana["base"], ana["cumplimiento"], ana["pct_comision"], ana["comision"]) == (1600, 100, 2, 32)
    assert ana["manual"] == 30 and ana["diferencia"] == 2
    # Beto: 900 / 1000 = 90 % → 1 % = 9, más ajuste de 5
    assert (beto["cumplimiento"], beto["comision_bruta"], beto["ajustes"], beto["comision"]) == (90, 9, 5, 14)
    # Carla (gerente): comisiona sobre toda la sucursal 1 (1600) contra la meta de sucursal (2000) → 80 % → 0.5 %
    assert (carla["base_tipo"], carla["base"], carla["cumplimiento"], carla["comision"]) == ("Sucursal", 1600, 80, 8)
    cards = {c.key: c.value for c in run().cards}
    assert cards["comision_total"] == 54 and cards["diferencia_manual"] == 2


def test_missing_goal_or_plan_is_flagged_not_guessed():
    configure(m=MesDatos(metas_vendedor={7: 1600}))
    rows = calc_rows(run())
    assert rows["Beto Ventas"]["comision"] == 0 and "Falta meta" in rows["Beto Ventas"]["observaciones"]
    get_plan_store("").save_plan(plan(vendedores=[]), "test")
    rows = calc_rows(run())
    assert rows["Ana Ventas"]["comision"] == 0
    assert "no configurado" in rows["Ana Ventas"]["observaciones"]


def test_default_plan_has_no_invented_rates():
    rows = calc_rows(run())
    assert all(r["comision"] == 0 for r in rows.values())
    assert any("no configurado" in r["observaciones"] for r in rows.values())


def test_occupancy_factor():
    p = plan(ocupacion=Ocupacion(activo=True, tramos=[Tramo(desde=0, valor=0.8), Tramo(desde=70, valor=1)]))
    invoices = [{"fecha_cobro": "2026-10-05", "base": 1600, "vendedor_id": 7, "vendedor": "Ana", "sucursal_id": 1,
                 "sucursal": "01 Monterrey"}]
    rows = calculate(p, {MONTH: MES}, invoices, lambda wh, month: 65.0 if wh == [10] else None, TODAY)
    ana = next(r for r in rows if r["vendedor_id"] == 7)
    assert ana["ocupacion"] == 65 and ana["factor"] == 0.8 and ana["comision"] == pytest.approx(32 * 0.8)
    # sucursal sin almacenes: factor 1 y aviso
    beto_inv = invoices + [{"fecha_cobro": "2026-10-05", "base": 1000, "vendedor_id": 8, "vendedor": "Beto",
                            "sucursal_id": 2, "sucursal": "02"}]
    beto = next(r for r in calculate(p, {MONTH: MES}, beto_inv, lambda wh, m: 65.0, TODAY) if r["vendedor_id"] == 8)
    assert beto["factor"] is None and "sin almacenes" in beto["observaciones"]


def test_partial_period_uses_full_month():
    configure()
    rows = calc_rows(_run_from(date(2026, 10, 20)))
    assert rows["Ana Ventas"]["base"] == 1600  # el cálculo toma octubre completo aunque el filtro inicie el 20


def _run_from(d0):
    odoo = FakeOdoo(build_data(), relations=RELATIONS)
    f = KpiFilters(date_from=d0, date_to=TODAY, compare="none")
    return asyncio.run(get_module_kpis(odoo, Cache(""), get_settings(), "comisiones", f, use_cache=False))


def test_plan_change_refreshes_without_requerying_odoo():
    calls = {"n": 0}

    class Counting(FakeOdoo):
        async def search_read(self, *a, **kw):
            calls["n"] += 1
            return await super().search_read(*a, **kw)

    cache = Cache("")
    f = KpiFilters(date_from=date(2026, 10, 1), date_to=TODAY, compare="none")
    odoo = Counting(build_data(), relations=RELATIONS)
    first = asyncio.run(get_module_kpis(odoo, cache, get_settings(), "comisiones", f))
    n = calls["n"]
    configure()
    second = asyncio.run(get_module_kpis(odoo, cache, get_settings(), "comisiones", f))
    assert calls["n"] == n  # la parte de Odoo (Fase 01) sale de caché
    assert {c.key: c.value for c in first.cards}["comision_total"] == 0
    assert {c.key: c.value for c in second.cards}["comision_total"] == 54


def test_plan_validation():
    with pytest.raises(ValidationError):
        plan(vendedores=[Vendedor(odoo_id=9, nombre="X", puesto="gerente")])  # gerente sin sucursal
    with pytest.raises(ValidationError):
        Puesto(key="a1", label="A", tramos=[Tramo(desde=10, valor=1), Tramo(desde=10, valor=2)])
    with pytest.raises(ValidationError):
        plan(vendedores=[Vendedor(odoo_id=7, nombre="X", puesto="no_existe")])


def test_commission_table_not_sent_to_ai():
    from app.ai.anonymizer import Anonymizer, ai_payload

    configure()
    text = str(ai_payload(run(), Anonymizer(True)))
    assert "Ana Ventas" not in text and "ana@prosic.com" not in text


# ------------------------------------------------------------------ API y estado de cuenta
def client():
    app.dependency_overrides[get_odoo] = lambda: FakeOdoo(build_data(), relations=RELATIONS)
    return TestClient(app)


def login(c, user, pw):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_api_admin_only_and_roundtrip():
    with client() as c:
        assert c.get("/api/comisiones/plan", headers=login(c, "ana", "ana123")).status_code == 403
        h = login(c, "admin", "admin123")
        r = c.get("/api/comisiones/plan", headers=h)
        assert r.status_code == 200 and [p["key"] for p in r.json()["plan"]["puestos"]] == ["ejecutivo", "asesor", "gerente"]
        r = c.put("/api/comisiones/plan", headers=h, json=plan().model_dump(mode="json"))
        assert r.status_code == 200, r.text
        assert r.json()["version"] >= 1
        bad = plan().model_dump(mode="json")
        bad["vendedores"][0]["puesto"] = "nada"
        assert c.put("/api/comisiones/plan", headers=h, json=bad).status_code == 422
        r = c.put(f"/api/comisiones/mes/{MONTH}", headers=h, json=MES.model_dump(mode="json"))
        assert r.status_code == 200, r.text
        assert r.json()["datos"]["metas_vendedor"] == {"7": 1600.0, "8": 1000.0}
        assert c.get("/api/comisiones/mes/2026-13", headers=h).status_code == 422


def test_statement_preview_for_seller_and_manager():
    configure()
    with client() as c:
        h = login(c, "admin", "admin123")
        r = c.get("/api/comisiones/estado-cuenta", headers=h, params={"mes": MONTH, "vendedor_id": 7})
        assert r.status_code == 200, r.text
        assert "Ana Ventas" in r.text and "$32.00" in r.text and "FMTY/1" in r.text and "FMTY/7" not in r.text
        r = c.get("/api/comisiones/estado-cuenta", headers=h, params={"mes": MONTH, "vendedor_id": 9})
        assert "Tu equipo" in r.text and "Ana Ventas" in r.text  # el gerente ve a su equipo y las facturas de la sucursal
        assert c.get("/api/comisiones/estado-cuenta", headers=h, params={"mes": MONTH, "vendedor_id": 99}).status_code == 404
        # sin SMTP no envía
        assert c.post("/api/comisiones/enviar", headers=h, params={"mes": MONTH}).status_code == 400


def test_previous_period_is_calendar_month():
    def prev(a, b):
        p = KpiFilters(date_from=date.fromisoformat(a), date_to=date.fromisoformat(b)).previous()
        return p.date_from.isoformat(), p.date_to.isoformat()
    assert prev("2026-10-01", "2026-10-31") == ("2026-09-01", "2026-09-30")  # antes: 31/08–30/09
    assert prev("2026-03-01", "2026-03-31") == ("2026-02-01", "2026-02-28")
    assert prev("2026-11-01", "2026-11-18") == ("2026-10-01", "2026-10-18")  # mes en curso
    assert prev("2026-01-01", "2026-03-31") == ("2025-10-01", "2025-12-31")  # trimestre
    assert prev("2026-10-10", "2026-10-19") == ("2026-09-30", "2026-10-09")  # rango libre: N días atrás


def test_flat_rate_needs_no_goal():
    p = plan(puestos=[Puesto(key="ejecutivo", label="Ejecutivo", tramos=[Tramo(desde=0, valor=0.41)]),
                      Puesto(key="gerente", label="Gerente", base="sucursal", tramos=[Tramo(desde=0, valor=0.2)])])
    configure(p, MesDatos())
    rows = calc_rows(run())
    assert rows["Ana Ventas"]["comision"] == pytest.approx(1600 * 0.0041)
    assert rows["Carla Gerente"]["comision"] == pytest.approx(1600 * 0.002)  # 0.2 % de toda la sucursal 1
    assert rows["Ana Ventas"]["cumplimiento"] is None and not rows["Ana Ventas"]["observaciones"]
