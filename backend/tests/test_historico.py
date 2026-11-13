"""Histórico de flota: reconstrucción diaria desde el seguimiento de Odoo, promedios y permisos."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fake_odoo import FakeOdoo

from app.cache import Cache
from app.config import get_settings
from app.schemas.kpis import KpiFilters
from app.services import fleet_history
from app.services.fleet_history import _vehicle_days, get_fleet_store
from app.services.registry import get_module_kpis

TZ = ZoneInfo(get_settings().ODOO_TZ)
TODAY = datetime.now(TZ).date()


def utc_noon(days_ago: int) -> str:
    """Mediodía local de hace N días, expresado en UTC como lo guarda Odoo."""
    local = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), TZ).replace(hour=12)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fleet_data() -> dict:
    """V1 pasa de Disponible a Rentado hace 5 días; V2 siempre rentado y se cambia de almacén hace 3 días;
    V3 no es de renta (auto utilitario); V4 se archiva hace 2 días."""
    m = lambda i, n="x": [i, n]  # noqa: E731
    return {
        "fleet.vehicle.state": [{"id": 3, "name": "Available", "sequence": 0}, {"id": 2, "name": "To deliver", "sequence": 1},
                                {"id": 1, "name": "Rented", "sequence": 2}],
        "stock.warehouse": [
            {"id": 1, "name": "Monterrey", "view_location_id": m(100), "rental_out_location_id": m(150), "active": True},
            {"id": 2, "name": "Saltillo", "view_location_id": m(200), "rental_out_location_id": m(250), "active": True}],
        "stock.location": [{"id": 101, "parent_path": "100/101/", "usage": "internal", "active": True},
                           {"id": 201, "parent_path": "200/201/", "usage": "internal", "active": True},
                           {"id": 9, "parent_path": "9/", "usage": "customer", "active": True}],
        "product.product": [{"id": 70, "rental_ok": True, "active": True, "default_code": "TIJ 19"},
                            {"id": 71, "rental_ok": False, "active": True, "default_code": False}],
        "fleet.vehicle": [
            {"id": 1, "name": "GENIE/GS1930/A1", "lot_id": m(11, "A1"), "product_id": m(70, "[T] Tijera 19 pies"),
             "model_id": m(5, "GENIE/GS1930"), "state_id": m(1, "Rented"), "active": True,
             "current_warehouse_id": m(1, "Monterrey"), "create_date": utc_noon(10),
             "license_plate": "A1", "serial_no": "GS1930 A1", "fuel_type": "electric",
             "cost_center_id": m(1, "01 Plataformas de Elevación"), "owner_id": m(1, "PROSIC"), "subasta": False,
             "state_date": (TODAY - timedelta(days=5)).isoformat()},
            {"id": 2, "name": "JLG/450AJ/B2", "lot_id": m(12, "B2"), "product_id": m(70, "[T] Tijera 19 pies"),
             "model_id": m(6, "JLG/450AJ"), "state_id": m(1, "Rented"), "active": True,
             "current_warehouse_id": m(2, "Saltillo"), "create_date": utc_noon(10),
             "license_plate": "B2", "serial_no": "450AJ B2", "fuel_type": "diesel",
             "cost_center_id": m(1, "01 Plataformas de Elevación"), "owner_id": m(2, "RENTAL ZONE"), "subasta": True,
             "state_date": (TODAY - timedelta(days=7)).isoformat()},
            {"id": 3, "name": "VW/Saveiro", "lot_id": m(13, "C3"), "product_id": m(71, "Saveiro"),
             "model_id": m(7, "VW/Saveiro"), "state_id": m(1, "Rented"), "active": True,
             "current_warehouse_id": m(1, "Monterrey"), "create_date": utc_noon(10)},
            {"id": 4, "name": "GENIE/Z45/D4", "lot_id": m(14, "D4"), "product_id": m(70, "[T] Tijera 19 pies"),
             "model_id": m(8, "GENIE/Z45"), "state_id": m(3, "Available"), "active": False,
             "current_warehouse_id": m(1, "Monterrey"), "create_date": utc_noon(10)},
        ],
        "ir.model.fields": [{"id": 900, "model": "fleet.vehicle", "name": "state_id"},
                            {"id": 901, "model": "fleet.vehicle", "name": "active"}],
        "mail.tracking.value": [
            {"id": 1, "field": 900, "mail_message_id": m(50), "old_value_integer": 3, "new_value_integer": 1,
             "create_date": utc_noon(5)},
            {"id": 2, "field": 901, "mail_message_id": m(51), "old_value_integer": 1, "new_value_integer": 0,
             "create_date": utc_noon(2)},
        ],
        "mail.message": [{"id": 50, "model": "fleet.vehicle", "res_id": 1}, {"id": 51, "model": "fleet.vehicle", "res_id": 4}],
        "stock.move.line": [
            {"id": 1, "state": "done", "lot_id": m(12), "location_dest_id": m(101), "date": utc_noon(10)},
            {"id": 2, "state": "done", "lot_id": m(12), "location_dest_id": m(9), "date": utc_noon(6)},  # a cliente: no cambia almacén
            {"id": 3, "state": "done", "lot_id": m(12), "location_dest_id": m(201), "date": utc_noon(3)},
        ],
    }


@pytest.fixture()
def synced(tmp_path):
    settings = get_settings().model_copy(update={"HISTORY_DB_PATH": str(tmp_path / "h.db")})
    odoo = FakeOdoo(fleet_data())
    result = asyncio.run(fleet_history.sync(odoo, settings))
    return settings, odoo, result


def kpis(settings, odoo, days=10, **filters):
    f = KpiFilters(date_from=TODAY - timedelta(days=days), date_to=TODAY, **filters)
    return asyncio.run(get_module_kpis(odoo, Cache(""), settings, "historico", f, use_cache=False))


def test_vehicle_days_uses_state_at_end_of_day():
    d0 = date(2026, 1, 1)
    rows = list(_vehicle_days(1, "2026-01-01 08:00:00", [("2026-01-03 10:00:00", 3, 1)], [("2026-01-05 09:00:00", 1, 0)],
                              [("2026-01-02 12:00:00", 2)], 1, False, 9, d0, date(2026, 1, 6), "2026-01-06 10:00:00"))
    # estado inicial = «anterior» del primer cambio; almacén inicial = el del primer movimiento; archivado el día 5;
    # el estado vigente empieza en el alta y después en el día del cambio
    assert rows == [("2026-01-01", 1, 3, 2, "2026-01-01"), ("2026-01-02", 1, 3, 2, "2026-01-01"),
                    ("2026-01-03", 1, 1, 2, "2026-01-03"), ("2026-01-04", 1, 1, 2, "2026-01-03")]


def test_vehicle_days_without_changes_uses_odoo_state_date():
    rows = list(_vehicle_days(1, "2026-01-01 08:00:00", [], [], [], 3, True, 9, date(2026, 1, 1), date(2026, 1, 3),
                              "2026-01-03 10:00:00", "2026-01-02"))
    assert [r[4] for r in rows] == ["2026-01-01", "2026-01-02", "2026-01-02"]


def test_sync_rebuilds_daily_history(synced):
    settings, _, result = synced
    assert result["events"] == 2 and result["vehicles"] == 4
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    by_day = {}
    for day, wh, st, n in store.counts(TODAY - timedelta(days=10), TODAY, None):  # solo flota de renta (sin V3)
        by_day.setdefault(day, {}).setdefault(st, 0)
        by_day[day][st] += n
    d = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731
    assert by_day[d(10)] == {3: 2, 1: 1}  # V1 y V4 disponibles, V2 rentado
    assert by_day[d(5)] == {3: 1, 1: 2}  # V1 ya rentado
    assert by_day[d(2)] == {1: 2}  # V4 archivado
    wh_today = {wh: n for day, wh, st, n in store.counts(TODAY, TODAY, None)}
    assert wh_today == {1: 1, 2: 1}  # V2 se movió a Saltillo; el movimiento a cliente no cambió su almacén


def test_sync_is_incremental(synced):
    settings, odoo, _ = synced
    odoo.data["mail.tracking.value"].append({"id": 3, "field": 900, "mail_message_id": [52, "x"], "old_value_integer": 1,
                                            "new_value_integer": 2, "create_date": utc_noon(0)})
    odoo.data["mail.message"].append({"id": 52, "model": "fleet.vehicle", "res_id": 2})
    again = asyncio.run(fleet_history.sync(odoo, settings))
    assert again["events"] == 1 and again["rebuilt_from"] == TODAY.isoformat()


def test_historico_module_averages(synced):
    settings, odoo, _ = synced
    k = kpis(settings, odoo)
    c = {x.key: x for x in k.cards}
    # ocupadas (unidades-día) 5*1 + 3*2 + 3*2 = 17 de una flota de 5*3 + 3*3 + 3*2 = 30
    assert c["ocupacion_prom"].value == pytest.approx(56.67, abs=0.01)
    assert c["flota_prom"].value == pytest.approx(30 / 11, abs=0.01)
    assert c["dias_periodo"].value == 11
    daily = next(t for t in k.tables if t.key == "historico_diario")
    assert daily.rows[0]["fecha"] == TODAY.isoformat() and daily.rows[0]["ocupacion"] == 100.0
    by_wh = {r["label"]: r for r in next(t for t in k.tables if t.key == "promedio_almacen").rows}
    assert set(by_wh) == {"Monterrey", "Saltillo"}
    assert any("Ocupación = (Por entregar, Rentado)" in n or "Rentado" in n for n in k.notes)


def test_historico_occupied_states_are_configurable(synced):
    settings, odoo, _ = synced
    s2 = settings.model_copy(update={"FLEET_OCCUPIED_STATES": "Disponible"})  # nombre en español también vale
    c = {x.key: x for x in kpis(s2, odoo).cards}
    assert c["ocupacion_prom"].value == pytest.approx(13 / 30 * 100, abs=0.01)


def test_historico_warehouse_filter(synced):
    settings, odoo, _ = synced
    c = {x.key: x for x in kpis(settings, odoo, warehouse_id=2).cards}
    assert c["flota_prom"].value == pytest.approx(4 / 11, abs=0.01)  # V2 en Saltillo desde hace 3 días (4 días contando hoy)
    assert c["ocupacion_prom"].value == 100.0


def test_historico_unavailable_before_first_sync(tmp_path):
    settings = get_settings().model_copy(update={"HISTORY_DB_PATH": str(tmp_path / "vacio.db")})
    k = kpis(settings, FakeOdoo(fleet_data()))
    assert not k.available and "primera vez" in (k.reason or "")


def _records(settings, fmt="diario", days=1, **flt):
    from app.routers.historico import cut_days, shape
    from app.services.historico import resolve_states

    store = get_fleet_store(settings.HISTORY_DB_PATH)
    occupied, _ = resolve_states(store.states(), settings)
    d_from = TODAY - timedelta(days=days)
    cuts = cut_days(d_from, TODAY) if fmt == "corte" else None
    raw = store.records(d_from, TODAY, None, flt, cuts)
    return store, occupied, raw, [shape(r, fmt, occupied) for r in raw]


def test_daily_structure_has_report_columns(synced):
    settings, _, _ = synced
    rows = _records(settings)[3]
    assert len(rows) == 2 * 2  # 2 días × 2 unidades de renta activas (sin el auto utilitario ni la archivada)
    b2 = next(r for r in rows if r["lote"] == "B2" and r["fecha"] == TODAY.isoformat())
    assert b2 == {
        "nombre": "B2", "lote": "B2", "producto": "Tijera 19 pies", "familia": "TIJ 19", "subfamilia": "TIJ 19. DIESEL",
        "almacen": "Saltillo", "estado": "Rentado", "fecha": TODAY.isoformat(), "disponibilidad": "No disponible",
        "centro": "01 Plataformas de Elevación", "propietario": "RENTAL ZONE", "centro2": "01 Plataformas de Elevación",
        "dia": TODAY.day, "date_duration": (TODAY - timedelta(days=7)).isoformat(), "duration_day": 7,
        "estado_d": "Rentado", "subasta": "Sí", "rentado": "Rentado"}
    a1 = next(r for r in rows if r["lote"] == "A1" and r["fecha"] == TODAY.isoformat())
    assert a1["date_duration"] == (TODAY - timedelta(days=5)).isoformat() and a1["duration_day"] == 5  # día del cambio
    assert a1["subfamilia"] == "TIJ 19. ELÉCTRICO"


def test_cut_days_are_month_ends():
    from app.routers.historico import cut_days

    assert cut_days(date(2026, 1, 15), date(2026, 3, 10)) == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 10)]


def test_cut_structure_rows(synced):
    from app.routers.historico import COLUMNS, cut_days

    settings, _, _ = synced
    rows = _records(settings, "corte", days=40)[3]
    assert {r["fecha"] for r in rows} <= {d.isoformat() for d in cut_days(TODAY - timedelta(days=40), TODAY)}
    today = [r for r in rows if r["fecha"] == TODAY.isoformat()]
    assert {r["nombre"] for r in today} == {"GS1930 A1", "450AJ B2"}  # en el corte, name = número de serie
    assert [label for _, label, _ in COLUMNS["corte"]] == [
        "name", "Lote/Nº de serie", "[Producto/Nombre]", "SubFamilia", "Almacen/Nombre del almacén", "Estado", "Fecha",
        "CentrDeCosto", "Propietario", "date_duration", "duration_day"]


def test_records_filters(synced):
    settings, _, _ = synced
    series = lambda **flt: {r["serie"] for r in _records(settings, days=10, **flt)[2]}  # noqa: E731
    assert series(owner="RENTAL ZONE") == {"B2"}
    assert series(subasta=False) == {"A1", "D4"}
    assert series(q="gs1930") == {"A1"}
    assert series(state_id=3) == {"A1", "D4"}  # A1 estuvo disponible antes de rentarse
    store = get_fleet_store(settings.HISTORY_DB_PATH)
    assert store.count_records(TODAY - timedelta(days=10), TODAY, None, {}) == 11 + 11 + 8  # D4 se archivó
    assert store.options()["familia"] == ["TIJ 19"] and store.options()["owner"] == ["PROSIC", "RENTAL ZONE"]


def test_export_csv_uses_readable_dates(synced):
    from app.routers.historico import _csv

    settings, _, _ = synced
    _, occupied, raw, _ = _records(settings)
    lines = "".join(_csv(raw, "diario", occupied)).lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("name,Lote/Nº de serie,[Producto/Nombre],Familia,SubFamilia,Almacen/Nombre del almacén")
    assert len(lines) == 1 + 4
    assert TODAY.strftime("%d/%m/%Y") in lines[-1] and TODAY.isoformat() not in "".join(lines[1:])
    assert "Saveiro" not in "".join(lines)


def test_roles_with_rentas_get_historico(tmp_path):
    """Una base creada antes de este módulo: los roles que veían «rentas» reciben «historico» al abrirla."""
    import sqlite3

    from app.store import Store

    path = str(tmp_path / "s.db")
    Store(path)
    c = sqlite3.connect(path)
    with c:
        c.execute("DELETE FROM settings WHERE k = '_mig_historico'")
        c.execute("UPDATE roles SET modules = ? WHERE name = 'consulta'", (json.dumps(["ventas", "rentas"]),))
        c.execute("INSERT INTO roles VALUES ('compras', 'Compras', ?, 0, 0)", (json.dumps(["compras"]),))
    c.close()
    s = Store(path)
    assert s.role("consulta").modules == ["ventas", "rentas", "historico"]
    assert s.role("compras").modules == ["compras"]
