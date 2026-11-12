from datetime import timedelta

from fastapi.testclient import TestClient
from fake_odoo import FakeOdoo
from sample_data import RELATIONS, TODAY, TYPES, base_data

from app.auth import hash_password, verify_password
from app.deps import get_odoo
from app.main import app


def client():
    app.dependency_overrides[get_odoo] = lambda: FakeOdoo(base_data(), TYPES, RELATIONS)
    return TestClient(app)


def login(c, user="ana", pw="ana123"):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_password_hash():
    h = hash_password("secreto", 1000)
    assert "$" not in h
    assert verify_password("secreto", h)
    assert not verify_password("otro", h)


def test_requires_auth():
    with client() as c:
        assert c.get("/api/kpis/ventas", params={"date_from": "2025-01-01", "date_to": "2025-01-31"}).status_code == 401


def test_bad_login():
    with client() as c:
        assert c.post("/api/auth/login", json={"username": "ana", "password": "x"}).status_code == 401


def test_kpis_endpoint():
    with client() as c:
        h = login(c)
        r = c.get("/api/kpis/ventas", headers=h, params={
            "date_from": (TODAY - timedelta(days=30)).isoformat(), "date_to": TODAY.isoformat()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        assert any(card["key"] == "ventas" for card in body["cards"])


def test_kpis_validates_range():
    with client() as c:
        h = login(c)
        r = c.get("/api/kpis/ventas", headers=h, params={"date_from": "2025-02-01", "date_to": "2025-01-01"})
        assert r.status_code == 422


def test_fields_endpoint_admin_only():
    with client() as c:
        assert c.get("/api/odoo/fields/maintenance.request", headers=login(c)).status_code == 403
        r = c.get("/api/odoo/fields/maintenance.request", headers=login(c, "admin", "admin123"))
        assert r.status_code == 200
        assert "equipment_id" in r.json()["fields"]
        assert c.get("/api/odoo/fields/no.existe", headers=login(c, "admin", "admin123")).status_code == 404


def test_filters_endpoint():
    with client() as c:
        r = c.get("/api/odoo/filters", headers=login(c))
        assert r.status_code == 200
        assert [w["name"] for w in r.json()["warehouses"]] == ["Monterrey", "Saltillo"]


def test_scoped_user_only_sees_own_warehouse():
    with client() as c:
        h = login(c, "gerente", "ger123")
        me = c.get("/api/auth/me", headers=h).json()
        assert me["warehouse_id"] == 2
        opts = c.get("/api/odoo/filters", headers=h).json()
        assert [w["name"] for w in opts["warehouses"]] == ["Saltillo"] and opts["scope"]["warehouse_id"] == 2
        assert opts["odoo_url"] == "https://odoo.test"
        # Aunque pida Monterrey (1) o ningún almacén, el backend fuerza Saltillo (2)
        for wh in ("1", None):
            params = {"date_from": (TODAY - timedelta(days=30)).isoformat(), "date_to": TODAY.isoformat()}
            if wh:
                params["warehouse_id"] = wh
            body = c.get("/api/kpis/rentas", headers=h, params=params).json()
            assert body["filters"]["warehouse_id"] == 2
            cards = {k["key"]: k["value"] for k in body["cards"]}
            assert cards["unidades_total"] == 10  # solo SAL/Stock


def test_summary_preview_admin_only():
    with client() as c:
        assert c.get("/api/admin/summary/preview", headers=login(c)).status_code == 403
        r = c.get("/api/admin/summary/preview", headers=login(c, "admin", "admin123"))
        assert r.status_code == 200
        assert "Resumen semanal" in r.text and "Rentas e inventario" in r.text
        assert c.get("/api/admin/summary/status", headers=login(c, "admin", "admin123")).json()["email_enabled"] is False
        assert c.post("/api/admin/summary/send", headers=login(c, "admin", "admin123")).status_code == 400


def test_ai_disabled_returns_503():
    with client() as c:
        r = c.post("/api/ai/analyze", headers=login(c), json={
            "modulo": "ventas", "filtros": {"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat()}})
        assert r.status_code == 503
