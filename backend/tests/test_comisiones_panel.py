"""Panel inteligente de comisiones: de lo cobrado a la comisión, qué revisar, cartera potencial y ficha por vendedor."""
import asyncio
from datetime import date

import pytest
from fake_odoo import FakeOdoo
from fastapi.testclient import TestClient
from test_comisiones import ACC, RELATIONS, TODAY, D, build_data
from test_comisiones_calculo import MES, configure, plan

from app.cache import Cache
from app.config import get_settings
from app.deps import get_odoo
from app.main import app
from app.schemas.kpis import KpiFilters
from app.services import commission_insights as ci
from app.services.commission_plan import Depuracion, Vendedor
from app.services.common import Ctx


def data():
    d = build_data()
    moves, lines = d["account.move"], d["account.move.line"]
    # F (FMTY/6, renta 400) tiene saldo: es cartera abierta de Ana
    next(m for m in moves if m["name"] == "FMTY/6")["payment_state"] = "partial"

    def open_inv(mid, name, days, seller, amount):
        moves.append({"id": mid, "name": name, "move_type": "out_invoice", "invoice_date": D(days), "date": D(days),
                      "ref": "", "partner_id": [990 + mid, f"Cliente {mid}"], "invoice_user_id": seller,
                      "branch_office_id": [1, "01 Monterrey"], "payment_state": "not_paid",
                      "amount_total_signed": amount * 1.16, "amount_residual_signed": amount * 1.16,
                      "invoice_date_due": D(days - 30), "journal_id": [1, "x"], "state": "posted"})
        lines.append({"id": mid * 10, "move_id": [mid, name], "account_id": [ACC["renta"], "cta"], "debit": 0.0,
                      "credit": amount, "date": D(days), "partner_id": [990 + mid, "c"],
                      "cost_center_id": [1, "01 Plataformas de Elevación"], "product_id": False, "journal_id": [1, "x"],
                      "parent_state": "posted", "matched_credit_ids": []})
    open_inv(60, "FMTY/60", 340, [8, "Beto Ventas"], 3000.0)  # cumple 365 días pronto: en riesgo
    open_inv(61, "FMTY/61", 400, [7, "Ana Ventas"], 800.0)  # ya pasó de 365 días: no comisionará
    open_inv(62, "FMTY/62", 20, [99, "Zeta Nueva"], 1500.0)  # vendedora que no está en el plan
    return d


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(ci, "local_today", lambda ctx: TODAY)


def panel(**filters):
    odoo = FakeOdoo(data(), relations=RELATIONS)
    f = KpiFilters(date_from=date(2026, 10, 1), date_to=TODAY, compare="none", **filters)
    ctx = Ctx(odoo=odoo, filters=f, tz="America/Mexico_City", settings=get_settings(), cache=Cache(""))
    return asyncio.run(ci.build_panel(ctx))


def seller(p, name):
    return next(s for s in p["vendedores"] if s["nombre"] == name)


def test_funnel_explains_the_base():
    configure(plan(depuracion=Depuracion(cuentas=["401-001-001"])))
    p = panel()
    steps = {s["key"]: s["monto"] for s in p["embudo"]}
    # saldadas en octubre: A 1000 + B 500 + C 300 (póliza) + D 200 (+365) + E 100 (maniobra) + G 900
    assert steps["saldado"] == 3000 and steps["auto"] == -500 and steps["reglas"] == 0
    assert steps["cuentas"] == -100  # la maniobra no está en las cuentas que comisionan
    assert steps["base"] == 2400 and steps["saldado"] + steps["auto"] + steps["reglas"] + steps["cuentas"] == steps["base"]
    assert steps["comision"] == p["resumen"]["comision"]


def test_open_portfolio_potential_and_risk():
    configure()
    p = panel()
    ana, beto = seller(p, "Ana Ventas"), seller(p, "Beto Ventas")
    # Ana: FMTY/6 (400) abierta comisionaría a su 2 % del mes; FMTY/61 ya pasó de 365 días → perdida
    assert ana["potencial_base"] == 400 and ana["potencial_comision"] == 8 and ana["perdida_base"] == 800
    assert [f["factura"] for f in ana["abiertas_top"]] == ["FMTY/6"]
    # Beto: FMTY/60 (3000) cumple 365 días en menos de 60: en riesgo a su 1 %
    assert beto["riesgo_base"] == 3000 and beto["riesgo_comision"] == 30
    kinds = [a["nivel"] for a in p["alertas"]]
    assert kinds[0] == "riesgo" and "Comisión en riesgo" in p["alertas"][0]["titulo"]
    assert 8 in p["alertas"][0]["vendedores"]
    assert any("fuera del plan" in a["titulo"] for a in p["alertas"])  # Zeta (99) factura pero no está en el plan
    assert p["resumen"]["potencial_base"] == 400 + 3000 + 1500


def test_next_tier_opportunity_and_finance_difference():
    configure()
    p = panel()
    beto = seller(p, "Beto Ventas")  # 900 de 1000 → 90 % → 1 %; el siguiente tramo es 100 % → 2 %
    assert beto["siguiente"] == {"desde": 100, "valor": 2, "falta_base": 100, "comision_extra": 11}
    assert any(a["nivel"] == "oportunidad" and "Beto Ventas" in a["titulo"] for a in p["alertas"])
    assert any("Diferencias con el cálculo de Finanzas" in a["titulo"] for a in p["alertas"])  # Ana: 32 vs 30


def test_seller_card_has_invoices_and_tramos():
    configure()
    ana = seller(panel(), "Ana Ventas")
    assert ana["comision"] == 32 and ana["meta"] == 1600 and [t["valor"] for t in ana["tramos"]] == [0, 1, 2]
    assert {f["factura"] for f in ana["facturas_top"]} == {"FMTY/1", "FMTY/2", "FMTY/5"}


def test_mojibake_names_are_repaired():
    p = plan()
    p.vendedores[0] = Vendedor(odoo_id=7, nombre="Ana NuÃ±ez", puesto="ejecutivo", sucursal_id=1)
    assert p.vendedores[0].nombre == "Ana Nuñez"
    configure(p)
    assert seller(panel(), "Ana Nuñez")["comision"] == 32


def test_panel_api_permissions():
    configure(m=MES)
    app.dependency_overrides[get_odoo] = lambda: FakeOdoo(data(), relations=RELATIONS)
    try:
        with TestClient(app) as c:
            def h(u, pw):
                return {"Authorization": f"Bearer {c.post('/api/auth/login', json={'username': u, 'password': pw}).json()['access_token']}"}
            q = {"date_from": "2026-10-01", "date_to": TODAY.isoformat(), "compare": "none"}
            assert c.get("/api/comisiones/panel", params=q).status_code == 401
            r = c.get("/api/comisiones/panel", params=q, headers=h("admin", "admin123"))
            assert r.status_code == 200 and r.json()["embudo"][0]["key"] == "saldado"
            # el gerente está limitado a un almacén: comisiones no se filtra por almacén → no ve datos globales
            assert c.get("/api/comisiones/panel", params=q, headers=h("gerente", "ger123")).status_code == 409
    finally:
        app.dependency_overrides.pop(get_odoo, None)
