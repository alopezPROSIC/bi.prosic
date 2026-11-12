import asyncio
from datetime import timedelta

from fake_odoo import FakeOdoo
from sample_data import RELATIONS, TODAY, TYPES, base_data, dt

from app.cache import Cache
from app.config import get_settings
from app.schemas.kpis import KpiFilters
from app.services.registry import get_module_kpis


def run(module, data=None, **filters):
    odoo = FakeOdoo(data or base_data(), TYPES, RELATIONS)
    f = KpiFilters(date_from=TODAY - timedelta(days=30), date_to=TODAY, **filters)
    cache = Cache(get_settings().REDIS_URL)
    return asyncio.run(get_module_kpis(odoo, cache, get_settings(), module, f, use_cache=False))


def cards(k):
    return {c.key: c for c in k.cards}


def test_ventas_totals_and_ticket():
    k = run("ventas")
    assert k.available
    c = cards(k)
    assert c["ventas"].value == 4000  # excluye la orden cancelada
    assert c["pedidos"].value == 2
    assert c["ticket_promedio"].value == 2000
    assert c["notas_credito"].value == 200
    assert c["cobrado"].value == 500
    assert c["facturado"].value == 2800  # 3000 - 200 (la de hace 80 días queda fuera del periodo)


def test_ventas_aging_buckets():
    k = run("ventas")
    c = cards(k)
    assert c["cxc_total"].value == 1160 + 3480
    assert c["cxc_vencida"].value == 1160
    aging = next(ch for ch in k.charts if ch.key == "cxc_antiguedad")
    buckets = {row["label"]: row["valor"] for row in aging.data}
    assert buckets["31-60 días"] == 1160
    assert buckets["Por vencer"] == 3480
    table = next(t for t in k.tables if t.key == "cxc_por_tercero")
    assert table.rows[0]["label"] == "Constructora Alfa SA"


def test_ventas_branch_filter_and_payment_note():
    k = run("ventas", branch_id=7)
    assert cards(k)["ventas"].value == 1000
    assert any("sucursal no aplica" in n for n in k.notes)


def test_ventas_top_charts_mark_entities():
    k = run("ventas")
    top = next(ch for ch in k.charts if ch.key == "top_clientes")
    assert top.entity == "cliente"
    assert top.data[0] == {"label": "Grupo Beta", "valor": 3000.0}


def test_rentas_snapshot_by_warehouse():
    k = run("rentas")
    assert k.available
    c = cards(k)
    assert c["unidades_total"].value == 21
    assert c["unidades_rentadas"].value == 5
    assert c["unidades_mantenimiento"].value == 2
    assert c["unidades_disponibles"].value == 14  # en PROSIC «Rent In» es stock disponible
    assert "unidades_retorno" not in c  # sin ubicaciones de inspección configuradas
    assert c["vencen_7"].value == 1
    t = next(t for t in k.tables if t.key == "rentas_por_vencer")
    assert t.ai_include is False and t.rows[0]["_id"] == 1 and t.odoo_model == "rental.order.contract"


def test_rentas_days_in_location_and_idle():
    k = run("rentas")
    c = cards(k)
    # B45-001 entró a taller hace 40 días (se ignora su movimiento anterior a otra ubicación)
    assert c["cuarentena_dias_prom"].value == 40
    q = next(t for t in k.tables if t.key == "cuarentena_detalle")
    assert q.rows[0]["serie"] == "B45-001" and q.rows[0]["dias"] == 40 and q.ai_include is False
    # Ociosos: T19-001 (3 uds, 45 días). B45-002 llegó hace 5 días: no es ociosa.
    assert c["equipos_ociosos"].value == 3
    idle = next(t for t in k.tables if t.key == "ociosos_detalle")
    assert [r["serie"] for r in idle.rows] == ["T19-001"]


def test_rentas_returns_profitability_and_warehouse_table():
    k = run("rentas")
    ret = next(ch for ch in k.charts if ch.key == "retornos_esperados")
    assert ret.drill == "warehouse_id" and ret.data[0]["label"] == "Monterrey" and ret.data[0]["_id"] == 1
    prof = {r["modelo"]: r for r in next(t for t in k.tables if t.key == "rentabilidad_modelo").rows}
    assert prof["Tijera 19ft"]["unidades"] == 9
    assert prof["Tijera 19ft"]["ingreso"] > 0 and prof["Tijera 19ft"]["costo_reparacion"] == 2500
    wh = {r["label"]: r for r in next(t for t in k.tables if t.key == "comparativo_almacen").rows}
    assert wh["Monterrey"]["retornos_30"] == 1 and wh["Monterrey"]["_id"] == 1
    assert wh["Saltillo"]["utilizacion"] == 0


def test_rentas_return_status_when_configured(monkeypatch):
    from app import odoo_mapping as M

    monkeypatch.setattr(M, "RETURN_LOCATION_KEYWORDS", ["Rent In"])
    k = run("rentas")
    c = cards(k)
    assert c["unidades_retorno"].value == 1 and c["unidades_disponibles"].value == 13
    wh = {r["label"]: r for r in next(t for t in k.tables if t.key == "comparativo_almacen").rows}
    assert wh["Monterrey"]["retorno"] == 1


def test_rentas_history_records_and_charts(tmp_path):
    from app.config import get_settings
    from app.services.history import get_history

    settings = get_settings().model_copy(update={"HISTORY_DB_PATH": str(tmp_path / "h.db")})
    hist = get_history(settings.HISTORY_DB_PATH)
    asyncio.run(hist.record_fleet(TODAY - timedelta(days=1), [(1, "Monterrey", "rentado", 2), (1, "Monterrey", "disponible", 8)]))
    odoo = FakeOdoo(base_data(), TYPES, RELATIONS)
    f = KpiFilters(date_from=TODAY.replace(day=1), date_to=TODAY)
    k = asyncio.run(get_module_kpis(odoo, Cache(""), settings, "rentas", f, use_cache=False))
    series = asyncio.run(hist.fleet_series(TODAY - timedelta(days=5), TODAY, None))
    assert len(series) == 2 and series[TODAY.isoformat()]["rentado"] == 5  # se guardó la foto de hoy
    chart = next(ch for ch in k.charts if ch.key == "historial_flota")
    assert chart.data[0]["utilizacion"] == 20.0


def test_forecast_seasonal_method():
    from app.services.pronostico import forecast

    # 2 años con estacionalidad (diciembre alto) y +10% de crecimiento interanual
    year1 = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 300]
    year2 = [v * 1.1 for v in year1]
    preds, mape, method = forecast(year1 + year2, horizon=12)
    assert method.startswith("estacional")
    assert round(preds[0], 1) == 121.0  # enero año 3 = enero año 2 (110) × 1.1
    assert round(preds[11], 1) == 363.0  # diciembre conserva la estacionalidad
    assert mape is not None and mape < 1  # serie perfecta: error ~0

    short, mape_s, method_s = forecast([10, 20, 30], horizon=2)
    assert method_s.startswith("promedio") and short == [20.0, 20.0]
    assert forecast([0] * 24)[0] == [0.0] * 6


def test_pronostico_module():
    data = base_data()
    # historia de renta en meses cerrados (independiente de la fecha en que corre la prueba)
    for i, days in enumerate((40, 70, 100, 400), start=10):
        data["sale.report"].append({"id": i, "date": dt(days), "state": "sale", "price_subtotal": 5000.0,
                                    "partner_id": [100, "Constructora Alfa SA"], "user_id": [5, "Juan Vendedor"],
                                    "categ_id": [3, "Tijeras"], "product_id": [30, "Tijera 19ft"], "order_id": [1, "S001"],
                                    "warehouse_id": [1, "Monterrey"], "company_id": [1, "PROSIC"]})
    k = run("pronostico", data=data)
    assert k.available
    keys = {c.key for c in k.cards}
    assert {"pron_ventas_3m", "pron_renta_3m", "cxc_programada_90", "cxp_programada_90"} <= keys
    table = next(t for t in k.tables if t.key == "pronostico_mensual")
    assert len(table.rows) == 7 and "ventas_min" in table.rows[0]
    total = table.rows[-1]
    assert total["label"] == "Total 6 meses"
    assert total["ventas"] == round(sum(r["ventas"] for r in table.rows[:-1]), 2)
    assert any(ch.key == "calendario_flujo" for ch in k.charts)
    ventas = next(ch for ch in k.charts if ch.key == "pron_ventas")
    assert len(ventas.data) == 12 + 6


def test_tool_get_pronostico():
    payload, k = _tool_run("get_pronostico", {"sucursal": None, "almacen": None, "compania": None})
    assert k is not None and k.module == "pronostico" and "error" not in payload


def _tool_run(name, args):
    from app.ai.anonymizer import Anonymizer
    from app.ai.tools import run_tool
    from app.config import get_settings

    odoo = FakeOdoo(base_data(), TYPES, RELATIONS)
    return asyncio.run(run_tool(name, args, odoo=odoo, cache=Cache(""), settings=get_settings(), anon=Anonymizer(True)))


def test_ventas_dso():
    c = cards(run("ventas"))
    assert c["cxc_dso"].value is not None and c["cxc_dso"].value > 0


def test_rentas_unavailable_when_module_missing():
    data = base_data()
    del data["rental.order.contract"]
    k = run("rentas", data=data)
    assert k.available is False
    assert "rental.order.contract" in (k.reason or "")


def test_mantenimiento():
    k = run("mantenimiento")
    c = cards(k)
    assert c["peticiones"].value == 2
    assert c["abiertas"].value == 1
    assert c["cerradas"].value == 1
    assert c["correctivos_pct"].value == 50
    assert c["resolucion_dias"].value == 4
    assert c["costo_reparacion"].value == 2500


def test_mantenimiento_without_repair_module_degrades():
    data = base_data()
    del data["repair.order"]
    k = run("mantenimiento", data=data)
    assert k.available
    assert "costo_reparacion" not in cards(k)
    assert any("Costo de reparación" in n for n in k.notes)


def test_compras():
    k = run("compras")
    c = cards(k)
    assert c["gasto"].value == 1000
    assert c["concentracion_top1"].value == 80
    assert c["pendiente_recibir"].value == 400
    assert c["cxp_vencida"].value == 580


def test_previous_period_comparison():
    f = KpiFilters(date_from=TODAY.replace(day=1), date_to=TODAY, compare="previous")
    prev = f.previous()
    assert prev is not None
    assert prev.date_to == f.date_from - timedelta(days=1)
    assert (prev.date_to - prev.date_from) == (f.date_to - f.date_from)
