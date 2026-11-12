import asyncio
import json
from datetime import timedelta

from fake_odoo import FakeOdoo
from sample_data import RELATIONS, TODAY, TYPES, base_data

from app.ai.anonymizer import Anonymizer, ai_payload
from app.ai.chat import _split_safe
from app.ai.tools import TOOLS, run_tool
from app.cache import Cache
from app.config import get_settings
from app.schemas.kpis import KpiFilters
from app.services.registry import get_module_kpis

REAL_NAMES = ["Constructora Alfa SA", "Grupo Beta", "Juan Vendedor", "Ana Ventas"]


def kpis(module):
    odoo = FakeOdoo(base_data(), TYPES, RELATIONS)
    f = KpiFilters(date_from=TODAY - timedelta(days=30), date_to=TODAY)
    return asyncio.run(get_module_kpis(odoo, Cache(get_settings().REDIS_URL), get_settings(), module, f, use_cache=False))


def test_payload_strips_internal_ids_and_detail_tables():
    payload = ai_payload(kpis("rentas"), Anonymizer(True))
    text = json.dumps(payload, ensure_ascii=False)
    assert '"_id"' not in text  # los IDs de Odoo (enlaces / drill-down) no viajan a la IA
    assert "T19-001" not in text  # detalle por número de serie excluido
    assert "Equipos en mantenimiento (más antiguos primero)" in payload["omitido_por_privacidad"]
    assert any(t["clave"] == "comparativo_almacen" for t in payload["tablas"])


def test_payload_is_anonymized_and_aggregated():
    anon = Anonymizer(True)
    payload = json.dumps(ai_payload(kpis("ventas"), anon), ensure_ascii=False)
    for name in REAL_NAMES:
        assert name not in payload
    assert "CLIENTE-A" in payload and "VENDEDOR-A" in payload


def test_detail_tables_are_not_sent():
    anon = Anonymizer(True)
    payload = ai_payload(kpis("rentas"), anon)
    assert all(t["clave"] != "rentas_por_vencer" for t in payload["tablas"])
    assert "Rentas por vencer (próximos 30 días)" in payload["omitido_por_privacidad"]
    assert "Constructora Alfa SA" not in json.dumps(payload, ensure_ascii=False)


def test_deanonymize_roundtrip_and_state():
    anon = Anonymizer(True)
    a = anon.alias("cliente", "Grupo Beta")
    b = anon.alias("proveedor", "Refacciones Gama")
    text = f"{a} concentra la cobranza; {b} el gasto."
    assert anon.deanonymize(text) == "Grupo Beta concentra la cobranza; Refacciones Gama el gasto."
    restored = Anonymizer(True, anon.state())
    assert restored.alias("cliente", "Grupo Beta") == a
    assert restored.anonymize_text("¿Cómo va grupo beta?") == f"¿Cómo va {a}?"


def test_disabled_anonymizer_is_passthrough():
    anon = Anonymizer(False)
    assert anon.alias("cliente", "Grupo Beta") == "Grupo Beta"


def test_split_safe_never_cuts_alias():
    emit, rest = _split_safe("El cliente CLIEN")
    assert emit == "El cliente " and rest == "CLIEN"


def test_tools_schema_is_strict():
    for t in TOOLS:
        params = t["function"]["parameters"]
        assert t["function"]["strict"] is True
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])


def _tool(name, args):
    odoo = FakeOdoo(base_data(), TYPES, RELATIONS)
    return asyncio.run(run_tool(name, args, odoo=odoo, cache=Cache(get_settings().REDIS_URL), settings=get_settings(),
                                anon=Anonymizer(True)))


def test_tool_rejects_bad_dates():
    payload, k = _tool("get_ventas", {"fecha_desde": "2025-05-10", "fecha_hasta": "2025-01-01",
                                      "sucursal": None, "almacen": None, "compania": None})
    assert "error" in payload and k is None


def test_tool_resolves_names_and_reports_unknown():
    payload, k = _tool("get_utilizacion_rentas", {"fecha_desde": (TODAY - timedelta(days=30)).isoformat(),
                                                  "fecha_hasta": TODAY.isoformat(), "almacen": "monterrey",
                                                  "compania": None})
    assert k is not None and k.filters.warehouse_id == 1
    payload, k = _tool("get_compras", {"fecha_desde": TODAY.isoformat(), "fecha_hasta": TODAY.isoformat(),
                                       "sucursal": "Atlantida", "almacen": None, "compania": None})
    assert k is None and "Sucursal Norte" in payload["error"]


def test_cxc_tool_returns_only_receivables():
    payload, k = _tool("get_cxc", {"fecha_corte": None, "sucursal": None, "compania": None})
    assert k is not None
    assert all(c.key.startswith("cxc") for c in k.cards)
    assert {c.key for c in k.charts} == {"cxc_antiguedad"}


def test_unknown_tool():
    payload, k = _tool("borrar_todo", {})
    assert "error" in payload
