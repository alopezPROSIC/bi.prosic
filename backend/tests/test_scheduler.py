from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.registry import next_sync, parse_hhmm, seconds_until_sync
from app.services.scheduler import default_filters, target_day

MX = ZoneInfo("America/Mexico_City")


def settings(**kw):
    return get_settings().model_copy(update={"ODOO_TZ": "America/Mexico_City", **kw})


def test_odoo_se_actualiza_una_vez_al_dia_a_las_11_pm():
    s = settings(NIGHTLY_SYNC_TIME="23:00")
    assert next_sync(s, datetime(2026, 11, 17, 15, 51, tzinfo=MX)) == datetime(2026, 11, 17, 23, 0, tzinfo=MX)
    assert next_sync(s, datetime(2026, 11, 17, 23, 30, tzinfo=MX)) == datetime(2026, 11, 18, 23, 0, tzinfo=MX)


def test_cache_dura_hasta_la_corrida_nocturna():
    s = settings(NIGHTLY_SYNC_TIME="23:00", KPI_CACHE_TTL_SECONDS=300)
    # Una consulta de las 9:00 queda en caché hasta las 23:00 (14 h), no 5 minutos.
    assert seconds_until_sync(s, datetime(2026, 11, 17, 9, 0, tzinfo=MX)) == 14 * 3600 + 60


def test_actualizacion_nocturna_desactivable():
    assert parse_hhmm("") is None and parse_hhmm("25:00") is None
    s = settings(NIGHTLY_SYNC_TIME="", KPI_CACHE_TTL_SECONDS=300)
    assert next_sync(s) is None and seconds_until_sync(s) == 300


def test_la_corrida_nocturna_deja_lista_la_manana_siguiente():
    assert target_day(datetime(2026, 11, 17, 23, 0, tzinfo=MX)) == date(2026, 11, 18)
    assert target_day(datetime(2026, 11, 30, 23, 0, tzinfo=MX)) == date(2026, 12, 1)
    f = default_filters(settings(), date(2026, 12, 1))
    assert (f.date_from, f.date_to) == (date(2026, 12, 1), date(2026, 12, 1))
