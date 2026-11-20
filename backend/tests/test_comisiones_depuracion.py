"""Comisiones — depuración: cuentas que comisionan, traspasos, exclusiones/inclusiones y Excel depurado."""
import io
from datetime import date

import pytest
from pydantic import ValidationError
from test_comisiones import D
from test_comisiones_calculo import MES, MONTH, calc_rows, client, configure, login, plan, run

from app.services.commission_plan import Depuracion, Plan, Puesto, Regla, Tramo, depurar


def dep(**kw) -> Depuracion:
    return Depuracion(**kw)


def rule(**kw) -> Regla:
    base = {"id": "r" + str(abs(hash(kw.get("motivo", "x"))) % 10**8).zfill(8), "accion": "excluir", "motivo": "Prueba"}
    base.update(kw)
    return Regla(**base)


def depurated(k):
    return next(t for t in k.tables if t.key == "facturas_depuradas").rows


def test_only_rental_platforms_account_commissions():
    # Ana: renta 1000 + renta 500 + maniobra 100 → con solo 401-001-001 la maniobra no comisiona
    configure(plan(depuracion=dep(cuentas=["401-001-001"])))
    rows = calc_rows(run())
    assert rows["Ana Ventas"]["base"] == 1500
    # Beto: renta 1000 con NC de renta (402-001-001) por 100: el prorrateo la descuenta completa (toda su factura es renta)
    assert rows["Beto Ventas"]["base"] == 900
    by_inv = {r["factura"]: r for r in depurated(run())}
    assert by_inv["FMTY/5"]["estado"] == "Comisiona" and by_inv["FMTY/5"]["base_comisionable"] == 0
    assert "401-001-001: 1,000.00" in by_inv["FMTY/7"]["cuentas_txt"]
    # sin descontar notas de crédito en otras cuentas
    configure(plan(depuracion=dep(cuentas=["401-001-001"], notas_credito="ninguna")))
    assert calc_rows(run())["Beto Ventas"]["base"] == 1000


def test_credit_note_prorated_by_selected_share():
    inv = [{"factura": "X", "fecha_cobro": "2026-10-05", "base": 900, "vendedor_id": 7, "vendedor": "Ana",
            "sucursal_id": 1, "sucursal": "01", "cuentas": {"401-001-001": 600, "401-002-001": 400, "402-001-001": -100},
            "ingreso": {"401-001-001": 600, "401-002-001": 400}}]
    get = lambda mode: depurar(plan(depuracion=dep(cuentas=["401-001-001"], notas_credito=mode)), inv)[0]["base_comisionable"]  # noqa: E731
    assert (get("prorrateo"), get("todas"), get("ninguna")) == (540, 500, 600)


def test_puesto_can_use_its_own_accounts():
    p = plan(depuracion=dep(cuentas=["401-001-001"]))
    p.puestos[1].cuentas = ["401-%"]  # el gerente comisiona sobre todas las cuentas 401
    configure(p)
    rows = calc_rows(run())
    assert rows["Ana Ventas"]["base"] == 1500 and rows["Carla Gerente"]["base"] == 1600


def test_transfer_from_departed_seller():
    p = plan(depuracion=dep(reglas=[rule(accion="reasignar", motivo="Baja de Ana: su cartera pasa a Beto",
                                         vendedor_id=7, desde=date(2026, 10, 1), destino_vendedor_id=8)]))
    configure(p)
    rows = calc_rows(run())
    # las 3 facturas de Ana (1600) pasan a Beto (900 propias) → 2500; Ana ya no tiene base
    assert rows["Beto Ventas"]["base"] == 2500 and rows["Beto Ventas"]["facturas"] == 4
    assert "traspasada" in rows["Beto Ventas"]["observaciones"]
    assert "Ana Ventas" not in rows or rows["Ana Ventas"]["base"] == 0
    r = next(x for x in depurated(run()) if x["factura"] == "FMTY/1")
    assert (r["vendedor_original"], r["vendedor"], r["regla"]) == ("Ana Ventas", "Beto Ventas", "Baja de Ana: su cartera pasa a Beto")
    assert r["sucursal"] == "02 Tepotzotlán"  # toma la sucursal del vendedor destino
    # «Renglones con observaciones» no cuenta los traspasos (son informativos)
    assert {c.key: c.value for c in run().cards}["vendedores_pendientes"] == 0


def test_transfer_respects_dates_and_split():
    # solo lo cobrado desde el día 4 (FMTY/1 se cobró el 26, FMTY/2 el 28, FMTY/5 el 27) → todo; antes del 27 → nada
    p = plan(depuracion=dep(reglas=[rule(accion="reasignar", motivo="Cartera compartida", vendedor_id=7,
                                         facturas=["FMTY/1"], destino_vendedor_id=8, porcentaje=50)]))
    configure(p)
    rows = calc_rows(run())
    assert rows["Ana Ventas"]["base"] == 1100 and rows["Beto Ventas"]["base"] == 1400
    split = [x for x in depurated(run()) if x["factura"] == "FMTY/1"]
    assert sorted(x["participacion"] for x in split) == [50, 50]
    p = plan(depuracion=dep(reglas=[rule(accion="reasignar", motivo="Desde noviembre", vendedor_id=7,
                                         desde=date(2026, 11, 1), destino_vendedor_id=8)]))
    configure(p)
    assert calc_rows(run())["Ana Ventas"]["base"] == 1600


def test_exclude_and_include_rules():
    p = plan(depuracion=dep(reglas=[
        rule(accion="excluir", motivo="Cliente intercompañía", cliente="cliente 1"),
        rule(accion="incluir", motivo="Autorizado por Finanzas", facturas=["FMTY/4", "FMTY/3"]),
    ]))
    configure(p)
    rows = calc_rows(run())
    # FMTY/1 (Cliente 1, 1000) se excluye; FMTY/4 (+365 días, 200) se rescata; FMTY/3 (póliza) NO se rescata
    assert rows["Ana Ventas"]["base"] == 1600 - 1000 + 200
    by_inv = {r["factura"]: r for r in depurated(run())}
    assert by_inv["FMTY/1"]["motivo"] == "Regla: Cliente intercompañía"
    assert by_inv["FMTY/4"]["estado"] == "Comisiona" and by_inv["FMTY/4"]["regla"] == "Autorizado por Finanzas"
    assert by_inv["FMTY/3"]["estado"] == "No comisiona"
    effect = {r["regla"]: r for r in next(t for t in run().tables if t.key == "efecto_reglas").rows}
    # «contiene»: «Cliente 1» también atrapa a «Cliente 13» (FMTY/7 de Beto); para un cliente exacto usa folios
    assert effect["Cliente intercompañía"]["facturas"] == 2 and rows["Beto Ventas"]["base"] == 0


def test_max_days_is_configurable():
    configure(plan(depuracion=dep(max_dias=500)))
    assert calc_rows(run())["Ana Ventas"]["base"] == 1800  # FMTY/4 (400 días) ya comisiona
    configure(plan(depuracion=dep(max_dias=10)))
    assert calc_rows(run())["Ana Ventas"]["base"] < 1600


def test_rule_validation():
    with pytest.raises(ValidationError):
        Regla(id="abcd1234", accion="excluir", motivo="Sin condición")
    with pytest.raises(ValidationError):
        Regla(id="abcd1234", accion="reasignar", motivo="Sin destino", vendedor_id=7)
    with pytest.raises(ValidationError):
        Depuracion(cuentas=["401 001; DROP"])
    with pytest.raises(ValidationError):  # el destino debe estar en el plan
        plan(depuracion=dep(reglas=[rule(accion="reasignar", vendedor_id=7, destino_vendedor_id=999)]))
    # planes guardados antes de la depuración siguen siendo válidos
    old = plan().model_dump(mode="json")
    old.pop("depuracion")
    assert Plan.model_validate(old).depuracion.cuentas == []


def test_statement_follows_transfers():
    p = plan(depuracion=dep(reglas=[rule(accion="reasignar", motivo="Baja", vendedor_id=7, destino_vendedor_id=8)]))
    configure(p)
    with client() as c:
        h = login(c, "admin", "admin123")
        r = c.get("/api/comisiones/estado-cuenta", headers=h, params={"mes": MONTH, "vendedor_id": 8})
        assert r.status_code == 200 and "FMTY/1" in r.text and "FMTY/7" in r.text


def test_excel_download():
    from openpyxl import load_workbook
    configure(plan(depuracion=dep(cuentas=["401-001-001"], reglas=[
        rule(accion="reasignar", motivo="Baja", vendedor_id=7, destino_vendedor_id=8)])), MES)
    with client() as c:
        assert c.get("/api/comisiones/depurado.xlsx", headers=login(c, "ana", "ana123"),
                     params={"desde": MONTH, "hasta": MONTH}).status_code == 403
        h = login(c, "admin", "admin123")
        r = c.get("/api/comisiones/depurado.xlsx", headers=h, params={"desde": MONTH, "hasta": MONTH})
        assert r.status_code == 200, r.text
        wb = load_workbook(io.BytesIO(r.content))
        assert wb.sheetnames == ["Resumen por vendedor", "Facturas depuradas", "Efecto de reglas", "Reglas", "Parámetros"]
        ws = wb["Facturas depuradas"]
        head = [c.value for c in ws[1]]
        rows = [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]
        f1 = next(x for x in rows if x["Factura"] == "FMTY/1")
        assert (f1["Vendedor en Odoo"], f1["Comisiona a"], f1["Base comisionable"]) == ("Ana Ventas", "Beto Ventas", 1000)
        assert wb["Parámetros"]["B3"].value == "401-001-001"
        assert c.get("/api/comisiones/depurado.xlsx", headers=h,
                     params={"desde": "2026-10", "hasta": "2026-09"}).status_code == 400
        assert c.get("/api/comisiones/cuentas", headers=h).json()["cuentas"][0]["code"] == "401-001-001"


def test_tabulador_still_applies_after_cleanup():
    p = plan(depuracion=dep(cuentas=["401-001-001"]),
             puestos=[Puesto(key="ejecutivo", label="Ejecutivo", tramos=[Tramo(desde=0, valor=2)]),
                      Puesto(key="gerente", label="Gerente", base="sucursal", tramos=[Tramo(desde=0, valor=0.5)])])
    configure(p, MES)
    assert calc_rows(run())["Ana Ventas"]["comision"] == pytest.approx(1500 * 0.02)
    assert D(0) == "2026-10-31"
