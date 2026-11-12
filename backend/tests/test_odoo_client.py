import asyncio
import json

import httpx
import pytest

from app.config import get_settings
from app.odoo_client import OdooClient, OdooError, OdooForbiddenMethod, OdooModelUnavailable


def make_client(handler) -> OdooClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OdooClient(get_settings(), http=http)


def rpc_result(request: httpx.Request, result):
    body = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.mark.parametrize("method", ["create", "write", "unlink", "action_confirm", "button_validate", "read"])
def test_forbidden_methods_never_hit_network(method):
    calls = []

    def handler(request):
        calls.append(request)
        return rpc_result(request, 1)

    client = make_client(handler)
    with pytest.raises(OdooForbiddenMethod):
        asyncio.run(client.execute_kw("res.partner", method, [[]]))
    assert calls == []


def test_search_read_paginates_and_sends_context():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        args = body["params"]["args"]
        if body["params"]["service"] == "common":
            return rpc_result(request, 7)
        seen.append(args)
        kw = args[6]
        assert kw["context"]["lang"] == "en_US"
        offset, limit = kw["offset"], kw["limit"]
        total = 4500
        rows = [{"id": i} for i in range(offset, min(offset + limit, total))]
        return rpc_result(request, rows)

    client = make_client(handler)
    rows = asyncio.run(client.search_read("sale.order", [], ["id"]))
    assert len(rows) == 4500
    assert len(seen) == 3  # 2000 + 2000 + 500
    assert all(a[4] == "search_read" for a in seen)


def test_missing_model_raises_unavailable():
    def handler(request):
        body = json.loads(request.content)
        if body["params"]["service"] == "common":
            return rpc_result(request, 7)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
            "code": 200, "message": "Odoo Server Error",
            "data": {"name": "builtins.KeyError", "message": "'sale.rental.schedule'"}}})

    client = make_client(handler)
    with pytest.raises(OdooModelUnavailable):
        asyncio.run(client.fields_get("sale.rental.schedule"))


def test_retries_on_503_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503)
        return rpc_result(request, {"server_version": "15.0+e", "server_serie": "15.0"})

    client = make_client(handler)
    ver = asyncio.run(client.version())
    assert ver["server_serie"] == "15.0"
    assert attempts["n"] == 2


def test_gives_up_after_retries():
    def handler(request):
        return httpx.Response(502)

    client = make_client(handler)
    with pytest.raises(OdooError):
        asyncio.run(client.version())
