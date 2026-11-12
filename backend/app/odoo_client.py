"""Cliente de SOLO LECTURA para la API externa de Odoo (JSON-RPC sobre /jsonrpc).

Garantías:
- Solo se pueden invocar los métodos de ``ALLOWED_METHODS``. Cualquier otro
  (create, write, unlink, action_*, button_*, etc.) lanza ``OdooForbiddenMethod``
  ANTES de tocar la red.
- Timeouts y reintentos con backoff exponencial ante errores de red / 5xx.
- Concurrencia limitada con un semáforo para no saturar Odoo.
- ``search_read`` siempre paginado y con tope (ODOO_MAX_RECORDS).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("prosic.odoo")

ALLOWED_METHODS: frozenset[str] = frozenset({"search_read", "read_group", "search_count", "fields_get"})
_PAGE_SIZE = 2000
_RETRY_STATUS = {502, 503, 504, 429}


class OdooError(Exception):
    """Error genérico de Odoo."""


class OdooForbiddenMethod(OdooError):
    """Se intentó llamar un método fuera de la lista blanca."""


class OdooRpcError(OdooError):
    def __init__(self, name: str, message: str):
        super().__init__(f"{name}: {message}")
        self.name = name
        self.message = message


class OdooModelUnavailable(OdooError):
    """El modelo no existe (módulo no instalado) o el usuario no tiene acceso."""

    def __init__(self, model: str, reason: str):
        super().__init__(f"{model}: {reason}")
        self.model = model
        self.reason = reason


class OdooClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.ODOO_TIMEOUT_SECONDS, connect=10),
            headers={"Content-Type": "application/json"},
        )
        self._uid: int | None = None
        self._uid_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.ODOO_MAX_CONCURRENCY)
        self._ids = itertools.count(1)
        self._fields_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._unavailable: dict[str, str] = {}
        self.base_context = {"lang": "en_US", "tz": settings.ODOO_TZ}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ red
    async def _rpc(self, service: str, method: str, *args: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": next(self._ids),
            "params": {"service": service, "method": method, "args": list(args)},
        }
        attempts = max(1, self.s.ODOO_MAX_RETRIES)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._sem:
                    resp = await self._http.post(f"{self.s.ODOO_URL}/jsonrpc", json=payload)
                if resp.status_code in _RETRY_STATUS:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
                if body.get("error"):
                    data = body["error"].get("data") or {}
                    raise OdooRpcError(data.get("name", "OdooError"), data.get("message") or body["error"].get("message", ""))
                return body.get("result")
            except OdooRpcError:
                raise  # errores de negocio/permiso: no se reintentan
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                last_exc = exc
                if attempt == attempts:
                    break
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.3)
                log.warning("Odoo %s.%s falló (%s); reintento %d/%d en %.1fs", service, method, exc, attempt, attempts - 1, delay)
                await asyncio.sleep(delay)
        raise OdooError(f"No fue posible comunicarse con Odoo: {last_exc}")

    async def version(self) -> dict[str, Any]:
        return await self._rpc("common", "version")

    async def uid(self) -> int:
        if self._uid is not None:
            return self._uid
        async with self._uid_lock:
            if self._uid is None:
                uid = await self._rpc("common", "authenticate", self.s.ODOO_DB, self.s.ODOO_USER, self.s.ODOO_API_KEY, {})
                if not uid:
                    raise OdooError("Autenticación con Odoo rechazada: revisa ODOO_DB, ODOO_USER y ODOO_API_KEY")
                self._uid = int(uid)
        return self._uid

    async def execute_kw(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        if method not in ALLOWED_METHODS:
            raise OdooForbiddenMethod(f"Método '{method}' no permitido. Solo lectura: {sorted(ALLOWED_METHODS)}")
        kwargs = dict(kwargs or {})
        kwargs["context"] = {**self.base_context, **kwargs.get("context", {})}
        uid = await self.uid()
        try:
            return await self._rpc("object", "execute_kw", self.s.ODOO_DB, uid, self.s.ODOO_API_KEY, model, method, args, kwargs)
        except OdooRpcError as exc:
            reason = _unavailable_reason(exc, model)
            if reason:
                self._unavailable[model] = reason
                raise OdooModelUnavailable(model, reason) from exc
            raise

    # ------------------------------------------------------------ lectura
    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """search_read paginado. ``limit`` None => hasta ODOO_MAX_RECORDS."""
        cap = min(limit or self.s.ODOO_MAX_RECORDS, self.s.ODOO_MAX_RECORDS)
        out: list[dict[str, Any]] = []
        offset = 0
        while len(out) < cap:
            page = min(_PAGE_SIZE, cap - len(out))
            kw: dict[str, Any] = {"fields": fields, "offset": offset, "limit": page}
            if order:
                kw["order"] = order
            rows = await self.execute_kw(model, "search_read", [domain], kw)
            out.extend(rows)
            if len(rows) < page:
                break
            offset += page
        if len(out) >= self.s.ODOO_MAX_RECORDS:
            log.warning("search_read %s alcanzó el tope ODOO_MAX_RECORDS=%d; resultados truncados", model, cap)
        return out

    async def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str] | str,
        orderby: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kw: dict[str, Any] = {"lazy": False}
        if orderby:
            kw["orderby"] = orderby
        if limit:
            kw["limit"] = limit
        gb = [groupby] if isinstance(groupby, str) else groupby
        return await self.execute_kw(model, "read_group", [domain, fields, gb], kw)

    async def search_count(self, model: str, domain: list[Any]) -> int:
        return int(await self.execute_kw(model, "search_count", [domain]))

    async def fields_get(self, model: str) -> dict[str, dict[str, Any]]:
        if model in self._unavailable:
            raise OdooModelUnavailable(model, self._unavailable[model])
        if model not in self._fields_cache:
            self._fields_cache[model] = await self.execute_kw(
                model, "fields_get", [], {"attributes": ["string", "type", "store", "relation", "selection"]}
            )
        return self._fields_cache[model]

    async def missing_fields(self, model: str, fields: list[str]) -> list[str]:
        """Devuelve los campos (solo el primer segmento de rutas con punto) que no existen."""
        available = await self.fields_get(model)
        return [f for f in fields if f and f.split(".")[0] not in available]


def _unavailable_reason(exc: OdooRpcError, model: str) -> str | None:
    text = f"{exc.name} {exc.message}"
    if "KeyError" in exc.name and model in exc.message:
        return "El modelo no existe en esta instancia (¿módulo no instalado?)"
    if "Object" in text and ("doesn't exist" in text or "does not exist" in text):
        return "El modelo no existe en esta instancia (¿módulo no instalado?)"
    if "AccessError" in exc.name:
        return "El usuario de lectura no tiene permiso sobre este modelo"
    return None
