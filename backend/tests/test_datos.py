"""Datos de Odoo: extractor de consultas a la medida (vista previa, agrupación, Excel, permisos) y sincronización."""
import asyncio
import io
from datetime import date

import pytest
from fake_odoo import FakeOdoo
from fastapi.testclient import TestClient

from app.cache import Cache
from app.config import get_settings, runtime_settings
from app.deps import get_odoo
from app.main import app
from app.schemas.kpis import KpiFilters
from app.services import data_explorer as dx
from app.services import scheduler

TODAY = date(2026, 11, 19)
NORTE, SUR = [7, "Sucursal Norte"], [8, "Sucursal Sur"]


def inv(i, day, partner, branch, total, residual, kind="out_invoice", state="posted"):
    return {"id": i, "name": f"F-{i:03d}", "move_type": kind, "state": state, "invoice_date": day,
            "commercial_partner_id": partner, "branch_office_id": branch, "company_id": [1, "PROSIC"],
            "amount_total_signed": total, "amount_residual_signed": residual, "payment_state": "not_paid"}


def data():
    acme, beta = [1, "Acme SA"], [2, "Beta Renta"]
    return {"account.move": [
        inv(1, "2026-11-03", acme, NORTE, 1000.0, 1000.0),
        inv(2, "2026-11-10", acme, SUR, 500.0, 0.0),
        inv(3, "2026-11-12", beta, NORTE, 250.0, 250.0),
        inv(4, "2026-11-12", beta, NORTE, -50.0, 0.0, kind="out_refund"),
        inv(5, "2026-10-28", acme, NORTE, 9999.0, 9999.0),  # fuera del mes
        inv(6, "2026-11-05", beta, NORTE, 777.0, 0.0, kind="in_invoice"),  # proveedor: otra fuente
        inv(7, "2026-11-06", beta, NORTE, 333.0, 0.0, state="draft"),  # borrador: el filtro base lo quita
    ]}


TYPES = {"account.move": {"invoice_date": "date", "amount_total_signed": "monetary", "amount_residual_signed": "monetary",
                          "commercial_partner_id": "many2one", "branch_office_id": "many2one", "company_id": "many2one"}}


@pytest.fixture
def odoo():
    return FakeOdoo(data(), TYPES)


def settings(**kw):
    return get_settings().model_copy(update={"HISTORY_DB_PATH": "", **kw})


ALL = KpiFilters(date_from=TODAY, date_to=TODAY, compare="none")


def q(**kw):
    base = {"nombre": "Prueba", "fuente": "facturas_cliente", "periodo": {"preset": "mes_actual"},
            "columnas": [{"campo": "name"}, {"campo": "commercial_partner_id", "etiqueta": "Cliente"},
                         {"campo": "amount_total_signed"}]}
    return dx.Consulta.model_validate({**base, **kw})


def run(odoo, consulta, s=None, scope=ALL, cache=None, **kw):
    return asyncio.run(dx.run(odoo, cache or Cache(""), s or settings(), consulta, scope, today=TODAY, **kw))


def test_detail_rows_period_base_filter_and_labels(odoo):
    out = run(odoo, q(), preview=False)
    assert out["total"] == 4 and sorted(r["name"] for r in out["filas"]) == ["F-001", "F-002", "F-003", "F-004"]
    assert [c["label"] for c in out["columnas"]][1] == "Cliente" and out["columnas"][2]["type"] == "money"
    assert {r["commercial_partner_id"] for r in out["filas"]} == {"Acme SA", "Beta Renta"}  # nombre, no [id, nombre]
    assert out["totales"]["amount_total_signed"] == 1700.0 and out["periodo"] == {"desde": "2026-11-01", "hasta": "2026-11-19"}


def test_filters_rules(odoo):
    contains = q(filtros=[{"campo": "commercial_partner_id", "op": "contiene", "valor": "acme"}])
    assert run(odoo, contains, preview=False)["total"] == 2
    many = q(filtros=[{"campo": "branch_office_id", "op": "en", "valor": "norte, sur"},
                      {"campo": "amount_total_signed", "op": ">", "valor": "100"}])
    assert sorted(r["name"] for r in run(odoo, many, preview=False)["filas"]) == ["F-001", "F-002", "F-003"]
    paid = q(filtros=[{"campo": "amount_residual_signed", "op": "=", "valor": 0}])
    assert run(odoo, paid, preview=False)["total"] == 2
    custom = q(periodo={"preset": "personalizado", "desde": "2026-10-01", "hasta": "2026-10-31"})
    assert [r["name"] for r in run(odoo, custom, preview=False)["filas"]] == ["F-005"]


def test_group_and_sum(odoo):
    grouped = q(agrupar={"campos": ["commercial_partner_id"], "sumar": ["amount_total_signed", "amount_residual_signed"]})
    out = run(odoo, grouped, preview=False)
    assert [c["label"] for c in out["columnas"]][-1] == "Registros"
    assert out["filas"] == [  # ordenado por la primera suma, de mayor a menor
        {"g0": "Acme SA", "amount_total_signed": 1500.0, "amount_residual_signed": 1000.0, "__count": 2},
        {"g0": "Beta Renta", "amount_total_signed": 200.0, "amount_residual_signed": 250.0, "__count": 2},
    ]
    assert out["totales"]["amount_total_signed"] == 1700.0 and out["total"] == 4


def test_validation_protects_odoo(odoo):
    with pytest.raises(ValueError, match="no existe"):
        run(odoo, q(columnas=[{"campo": "campo_que_no_existe"}]))
    with pytest.raises(ValueError, match="necesita un periodo"):
        run(odoo, q(periodo={"preset": "sin_fecha"}))
    with pytest.raises(ValueError, match="no se puede usar"):
        run(odoo, q(agrupar={"campos": ["commercial_partner_id"], "sumar": ["name"]}))
    with pytest.raises(ValueError, match="3 años"):
        run(odoo, q(periodo={"preset": "personalizado", "desde": "2020-01-01", "hasta": "2026-01-01"}))
    with pytest.raises(dx.TooManyRows):
        run(odoo, q(), s=settings(DATA_EXPORT_MAX_ROWS=2), preview=False)
    assert run(odoo, q(), s=settings(DATA_EXPORT_MAX_ROWS=2), preview=True)["total"] == 4  # la vista previa sí


def test_scope_is_enforced(odoo):
    norte = KpiFilters(date_from=TODAY, date_to=TODAY, compare="none", allowed_branch_ids=[7], strict_scope=True)
    assert {r["name"] for r in run(odoo, q(), scope=norte, preview=False)["filas"]} == {"F-001", "F-003", "F-004"}
    almacen = KpiFilters(date_from=TODAY, date_to=TODAY, compare="none", warehouse_id=2, strict_scope=True)
    with pytest.raises(PermissionError):  # las facturas no tienen almacén: no se muestran datos de toda la empresa
        run(odoo, q(), scope=almacen)


def test_cache_and_throttled_refresh(odoo):
    cache = Cache("")
    run(odoo, q(), cache=cache)
    n = len(odoo.calls)
    assert run(odoo, q(), cache=cache)["desde_cache"] and len(odoo.calls) == n
    assert not run(odoo, q(), cache=cache, refresh=True)["desde_cache"]
    m = len(odoo.calls)
    assert run(odoo, q(), cache=cache, refresh=True)["desde_cache"] and len(odoo.calls) == m  # una vez cada 10 min


def test_saved_queries_store():
    st = dx.QueryStore("")
    seeded = st.visible("ana")
    assert len(seeded) == len(dx.STARTERS) and all(r["propietario"] == dx.SYSTEM_OWNER for r in seeded)
    mine = st.save(q(compartida=False), "ana", False)
    assert mine["propietario"] == "ana" and mine["id"] in {r["id"] for r in st.visible("ana")}
    assert mine["id"] not in {r["id"] for r in st.visible("beto")}  # privada
    with pytest.raises(PermissionError):
        st.save(q(id=mine["id"], nombre="Cambio"), "beto", False)
    assert st.save(q(id=mine["id"], nombre="Cambio"), "admin", True)["propietario"] == "ana"
    with pytest.raises(PermissionError):
        st.delete(mine["id"], "beto", False)
    assert st.delete(mine["id"], "ana", False) and st.get(mine["id"]) is None


def test_starters_are_valid():
    for doc in dx.STARTERS:
        c = dx.Consulta.model_validate(doc)
        assert c.fuente in dx.SOURCES


def _client(odoo):
    app.dependency_overrides[get_odoo] = lambda: odoo
    return TestClient(app)


def _h(c, u, p):
    return {"Authorization": f"Bearer {c.post('/api/auth/login', json={'username': u, 'password': p}).json()['access_token']}"}


def test_api_run_excel_and_permissions(odoo):
    try:
        with _client(odoo) as c:
            admin = _h(c, "admin", "admin123")
            body = {"consulta": q().model_dump(mode="json"), "vista_previa": True,
                    "desde": "2026-11-01", "hasta": "2026-11-19"}
            r = c.post("/api/datos/ejecutar", json=body, headers=admin)
            assert r.status_code == 200 and r.json()["total"] == 4
            r = c.post("/api/datos/excel", json=body, headers=admin)
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(r.content))
            assert wb.sheetnames == ["Datos", "Parámetros"] and wb["Datos"]["B1"].value == "Cliente"
            assert wb["Datos"].max_row == 4 + 3  # encabezado + 4 filas + espacio + total
            assert c.get("/api/datos/fuentes/facturas_cliente/campos", headers=admin).json()["campos"]
            saved = c.put("/api/datos/consultas", json=q().model_dump(mode="json"), headers=admin)
            assert saved.status_code == 200 and saved.json()["editable"]
            bad = c.put("/api/datos/consultas", json=q(columnas=[{"campo": "nada"}]).model_dump(mode="json"), headers=admin)
            assert bad.status_code == 400
            # el gerente solo ve el almacén 2: las facturas no se pueden filtrar por almacén
            ger = _h(c, "gerente", "ger123")
            assert c.post("/api/datos/ejecutar", json=body, headers=ger).status_code == 403
            fuentes = {f["key"]: f for f in c.get("/api/datos/fuentes", headers=ger).json()["fuentes"]}
            assert not fuentes["facturas_cliente"]["disponible"] and fuentes["flota"]["disponible"]
            # sin el permiso «datos» no hay acceso
            from app.store import Role, get_store
            st = get_store("")
            role = next(r for r in st.roles() if r.name == "direccion")
            st.upsert_role(Role(role.name, role.label, [m for m in role.modules if m != "datos"], role.ai, role.admin))
            assert c.get("/api/datos/consultas", headers=_h(c, "ana", "ana123")).status_code == 403
            assert c.get("/api/datos/sincronizacion", headers=_h(c, "ana", "ana123")).status_code == 403
    finally:
        app.dependency_overrides.pop(get_odoo, None)


def test_sync_panel_chooses_nightly_modules(odoo, monkeypatch):
    done = []

    async def fake_kpis(odoo, cache, settings, module, f, **kw):
        done.append(module)

    monkeypatch.setattr(scheduler, "get_module_kpis", fake_kpis)
    try:
        with _client(odoo) as c:
            admin = _h(c, "admin", "admin123")
            r = c.put("/api/datos/sincronizacion", json={"modulos": ["ventas", "rentas"]}, headers=admin)
            assert r.status_code == 200
            assert [m["key"] for m in r.json()["modulos"] if m["activo"]] == ["ventas", "rentas"]
            asyncio.run(scheduler.prefetch(odoo, Cache(""), runtime_settings(), TODAY))
            assert done == ["ventas", "rentas"]
            view = c.get("/api/datos/sincronizacion", headers=admin).json()
            ventas = next(m for m in view["modulos"] if m["key"] == "ventas")
            assert ventas["ultimo"]["ok"] and ventas["ultimo"]["tipo"] == "nocturna"
            assert c.post("/api/datos/sincronizacion/compras", headers=admin).status_code == 200
            assert done[-1] == "compras"
            assert c.post("/api/datos/sincronizacion/compras", headers=admin).status_code == 429  # no insiste
            assert c.put("/api/datos/sincronizacion", json={"modulos": ["x"]}, headers=admin).status_code == 400
    finally:
        app.dependency_overrides.pop(get_odoo, None)
