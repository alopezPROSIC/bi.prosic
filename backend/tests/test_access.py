"""Acceso con Google @prosic.com, aprobación de usuarios, roles, alcance por sucursal/almacén y configuración."""
import time
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fake_odoo import FakeOdoo
from fastapi.testclient import TestClient
from sample_data import RELATIONS, TODAY, TYPES, base_data

import app.google_auth as ga
import app.routers.auth as auth_router
from app.config import Settings
from app.deps import get_odoo
from app.google_auth import GoogleAuthError, GoogleIdentity
from app.main import app

PARAMS = {"date_from": (TODAY - timedelta(days=30)).isoformat(), "date_to": TODAY.isoformat()}


def client():
    app.dependency_overrides[get_odoo] = lambda: FakeOdoo(base_data(), TYPES, RELATIONS)
    return TestClient(app)


def admin_headers(c):
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def google(monkeypatch):
    """Simula el token de Google: el «credential» es el correo."""
    async def fake_verify(credential, client_id, domain):
        if not credential.endswith("@prosic.com"):
            raise GoogleAuthError("Solo se permiten cuentas de Google @prosic.com")
        return GoogleIdentity(email=credential, name=credential.split("@")[0].title(), domain="prosic.com")

    monkeypatch.setattr(auth_router, "verify_google_credential", fake_verify)


def google_login(c, email):
    return c.post("/api/auth/google", json={"credential": email})


def test_google_new_user_pending_then_approved(google):
    with client() as c:
        r = google_login(c, "nuevo.usuario@prosic.com")
        assert r.status_code == 403 and "pendiente" in r.json()["detail"]
        h = admin_headers(c)
        users = c.get("/api/admin/users", headers=h).json()["users"]
        assert users[0]["email"] == "nuevo.usuario@prosic.com" and users[0]["status"] == "pendiente"
        # el admin lo aprueba como gerente de Saltillo
        r = c.put("/api/admin/users", headers=h, json={"email": "nuevo.usuario@prosic.com", "name": "Nuevo",
                                                       "role": "gerente", "status": "activo", "warehouse_ids": [2]})
        assert r.status_code == 200, r.text
        r = google_login(c, "nuevo.usuario@prosic.com")
        assert r.status_code == 200
        me = r.json()["user"]
        assert me["role"] == "gerente" and "compras" not in me["modules"] and me["warehouse_ids"] == [2]
        events = [e["event"] for e in c.get("/api/admin/access-log", headers=h).json()["events"]]
        assert "registro" in events and "login" in events and "usuario_creado" not in events


def test_google_rejects_other_domain(google):
    with client() as c:
        r = google_login(c, "alguien.externo@gmail.com")
        assert r.status_code == 403 and "@prosic.com" in r.json()["detail"]


def test_bootstrap_admin_from_env(google):
    with client() as c:
        r = google_login(c, "jefe@prosic.com")
        assert r.status_code == 200 and r.json()["user"]["is_admin"] is True
        users = {u["email"]: u for u in c.get("/api/admin/users", headers=admin_headers(c)).json()["users"]}
        assert users["jefe@prosic.com"]["role"] == "admin"


def test_blocking_user_cuts_existing_session(google):
    with client() as c:
        h_admin = admin_headers(c)
        c.put("/api/admin/users", headers=h_admin, json={"email": "vendedor.uno@prosic.com", "role": "consulta",
                                                         "status": "activo"})
        token = google_login(c, "vendedor.uno@prosic.com").json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/auth/me", headers=h).status_code == 200
        c.put("/api/admin/users", headers=h_admin, json={"email": "vendedor.uno@prosic.com", "role": "consulta",
                                                         "status": "bloqueado"})
        r = c.get("/api/auth/me", headers=h)
        assert r.status_code == 401 and "bloqueado" in r.json()["detail"]


def test_role_permissions_enforced(google):
    with client() as c:
        c.put("/api/admin/users", headers=admin_headers(c), json={"email": "consulta.uno@prosic.com", "role": "consulta",
                                                                  "status": "activo"})
        h = {"Authorization": f"Bearer {google_login(c, 'consulta.uno@prosic.com').json()['access_token']}"}
        assert c.get("/api/kpis/ventas", headers=h, params=PARAMS).status_code == 200
        assert c.get("/api/kpis/compras", headers=h, params=PARAMS).status_code == 403
        r = c.post("/api/ai/analyze", headers=h, json={"modulo": "ventas", "filtros": PARAMS})
        assert r.status_code == 403  # el rol «consulta» no tiene IA
        assert c.get("/api/admin/users", headers=h).status_code == 403


def test_multi_warehouse_and_strict_branch_scope(google):
    with client() as c:
        h_admin = admin_headers(c)
        c.put("/api/admin/users", headers=h_admin, json={"email": "regional.norte@prosic.com", "role": "direccion",
                                                         "status": "activo", "warehouse_ids": [1, 2]})
        c.put("/api/admin/users", headers=h_admin, json={"email": "sucursal.norte@prosic.com", "role": "direccion",
                                                         "status": "activo", "branch_ids": [7]})
        h1 = {"Authorization": f"Bearer {google_login(c, 'regional.norte@prosic.com').json()['access_token']}"}
        body = c.get("/api/kpis/rentas", headers=h1, params=PARAMS).json()
        assert body["filters"]["allowed_warehouse_ids"] == [1, 2] and body["filters"]["strict_scope"] is True
        assert {k["key"]: k["value"] for k in body["cards"]}["unidades_total"] == 21  # MTY + SAL
        # pide un almacén fuera de su alcance -> se ignora
        body = c.get("/api/kpis/rentas", headers=h1, params={**PARAMS, "warehouse_id": 99}).json()
        assert body["filters"]["warehouse_id"] is None and body["filters"]["allowed_warehouse_ids"] == [1, 2]
        # solo con sucursal: el inventario (sin sucursal en Odoo) se oculta en lugar de mostrar toda la empresa
        h2 = {"Authorization": f"Bearer {google_login(c, 'sucursal.norte@prosic.com').json()['access_token']}"}
        body = c.get("/api/kpis/rentas", headers=h2, params=PARAMS).json()
        assert "unidades_total" not in {k["key"] for k in body["cards"]}
        assert any("se oculta para tu acceso" in n for n in body["notes"])
        # ventas sí se filtra por sucursal: solo el pedido de «Sucursal Norte»
        ventas = c.get("/api/kpis/ventas", headers=h2, params=PARAMS).json()
        assert {k["key"]: k["value"] for k in ventas["cards"]}["ventas"] == 1000


def test_cannot_remove_last_admin_or_admin_role():
    with client() as c:
        h = admin_headers(c)
        c.put("/api/admin/users", headers=h, json={"email": "unico.admin@prosic.com", "role": "admin", "status": "activo"})
        r = c.put("/api/admin/users", headers=h, json={"email": "unico.admin@prosic.com", "role": "consulta",
                                                       "status": "activo"})
        assert r.status_code == 400 and "último administrador" in r.json()["detail"]
        assert c.delete("/api/admin/roles/admin", headers=h).status_code == 400
        assert c.put("/api/admin/users", headers=h, json={"email": "x@gmail.com", "role": "consulta"}).status_code == 400


def test_custom_role_and_settings_override():
    with client() as c:
        h = admin_headers(c)
        r = c.put("/api/admin/roles", headers=h, json={"name": "cobranza", "label": "Cobranza", "modules": ["ventas"],
                                                       "ai": True})
        assert r.status_code == 200 and r.json()["modules"] == ["ventas"]
        r = c.put("/api/admin/settings", headers=h, json={"AI_ENABLED": False, "ALERT_AR_OVERDUE_MAX_PCT": 40})
        assert r.status_code == 200
        body = r.json()
        assert body["values"]["AI_ENABLED"] is False and body["values"]["ALERT_AR_OVERDUE_MAX_PCT"] == 40
        assert "ai_key_configured" in body["info"] and "OPENAI_API_KEY" not in str(body)
        assert c.put("/api/admin/settings", headers=h, json={"JWT_SECRET": "x"}).status_code == 400
        # volver al valor del .env
        assert c.put("/api/admin/settings", headers=h, json={"AI_ENABLED": None}).json()["values"]["AI_ENABLED"] is True


def test_auth_config_and_password_login_switch():
    assert Settings(GOOGLE_CLIENT_ID="abc.apps.googleusercontent.com").password_login is False
    assert Settings(GOOGLE_CLIENT_ID="abc", ALLOW_PASSWORD_LOGIN=True).password_login is True
    assert Settings(GOOGLE_CLIENT_ID="").password_login is True
    with client() as c:
        cfg = c.get("/api/auth/config").json()
        assert cfg["allowed_domain"] == "prosic.com" and cfg["password_login"] is True


def test_no_index_headers():
    with client() as c:
        r = c.get("/api/ping")
        assert "noindex" in r.headers["X-Robots-Tag"]


# ------------------------------------------------------------------ verificación real del token (firma RSA propia)
@pytest.fixture
def rsa_google(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJwks:
        def get_signing_key_from_jwt(self, token):
            class K:
                pass
            k = K()
            k.key = key.public_key()
            return k

    monkeypatch.setattr(ga, "_jwks", lambda: FakeJwks())

    def make(**claims):
        now = int(time.time())
        base = {"iss": "https://accounts.google.com", "aud": "cid", "iat": now, "exp": now + 600,
                "email": "ana.lopez@prosic.com", "email_verified": True, "hd": "prosic.com", "name": "Ana"}
        return jwt.encode({**base, **claims}, key, algorithm="RS256")
    return make


def test_verify_google_token(rsa_google):
    ident = ga._verify(rsa_google(), "cid", "prosic.com")
    assert ident.email == "ana.lopez@prosic.com"
    for bad in ({"hd": "otra.com"}, {"hd": None, "email": "ana@gmail.com"}, {"email_verified": False},
                {"aud": "otro-cliente"}, {"iss": "https://evil.com"}, {"exp": int(time.time()) - 10}):
        token = rsa_google(**{k: v for k, v in bad.items() if v is not None})
        with pytest.raises(GoogleAuthError):
            ga._verify(token, "cid", "prosic.com")
