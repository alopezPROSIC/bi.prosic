import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ODOO_URL", "https://odoo.test")
os.environ.setdefault("ODOO_DB", "test")
os.environ.setdefault("ODOO_USER", "bi@test")
os.environ.setdefault("ODOO_API_KEY", "key")
os.environ.setdefault("JWT_SECRET", "secreto-de-pruebas-muy-largo-123")
os.environ.setdefault("REDIS_URL", "")  # vacío: caché solo en memoria
os.environ.setdefault("ODOO_MAX_RETRIES", "2")
os.environ.setdefault("HISTORY_DB_PATH", "")  # sin SQLite salvo en las pruebas que lo piden
os.environ.setdefault("BACKGROUND_TASKS", "false")

from app.auth import hash_password  # noqa: E402

os.environ.setdefault("AUTH_USERS", ",".join([
    f"admin|admin|{hash_password('admin123', 1000)}",
    f"ana|viewer|{hash_password('ana123', 1000)}",
    f"gerente|viewer|{hash_password('ger123', 1000)}|almacen=2",  # solo ve el almacén 2 (Saltillo)
]))
os.environ.setdefault("GOOGLE_CLIENT_ID", "")
os.environ.setdefault("ALLOWED_EMAIL_DOMAIN", "prosic.com")
os.environ.setdefault("ADMIN_EMAILS", "jefe@prosic.com")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    """Cada prueba arranca con usuarios/configuración/bitácora vacíos y los roles por defecto."""
    import json

    from app.store import DEFAULT_ROLES, get_store

    store = get_store("")
    with store._lock, store._conn:  # noqa: SLF001
        for table in ("users", "settings", "access_log", "roles"):
            store._conn.execute(f"DELETE FROM {table}")  # noqa: SLF001
        for r in DEFAULT_ROLES:
            store._conn.execute("INSERT INTO roles VALUES (?, ?, ?, ?, ?)",  # noqa: SLF001
                                (r["name"], r["label"], json.dumps(r["modules"]), int(r["ai"]), int(r["admin"])))
    yield
