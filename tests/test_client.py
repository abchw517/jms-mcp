import json

import httpx
import pytest

from jms_mcp.client import JumpServerClient, combine_url
from jms_mcp.schema import compile_catalog


@pytest.mark.parametrize(
    "credential,scheme,expected",
    [
        ("abc", "Bearer", "Bearer abc"),
        ("abc", "Token", "Token abc"),
        ("Token abc", "Bearer", "Token abc"),
        ("bearer abc", "Token", "Bearer abc"),
    ],
)
def test_auth_normalization(settings, credential, scheme, expected):
    from pydantic import SecretStr

    settings.api_token = SecretStr(credential)
    settings.auth_scheme = scheme
    assert settings.authorization() == expected


@pytest.mark.parametrize("credential", ["", "Bearer", "Bearer a b", "a\r\nb"])
def test_bad_auth(settings, credential):
    from pydantic import SecretStr

    settings.api_token = SecretStr(credential)
    # A single word (including 'Bearer') can be a bare token; empty/multiword/control cannot.
    if credential == "Bearer":
        assert settings.authorization() == "Bearer Bearer"
    else:
        with pytest.raises(ValueError):
            settings.authorization()


async def test_auth_query_and_basepath(document, settings):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"count": 1, "results": [{"name": "host"}]})

    client = JumpServerClient(settings, transport=httpx.MockTransport(upstream))
    try:
        # Same credential must be used for Swagger and operation calls.
        await client.schema()
        result = await client.call(
            compile_catalog(document, settings),
            "assets_hosts_list",
            {"query": {"limit": 5, "is_active": True, "ids": ["a", "b"]}},
        )
        assert not result.isError
        assert requests[0].headers["Authorization"] == requests[1].headers["Authorization"]
        assert requests[1].url.path == "/api/v1/assets/hosts/"
        assert requests[1].url.params["is_active"] == "true"
        assert requests[1].url.params["ids"] == "a,b"  # Swagger default collectionFormat=csv.
    finally:
        await client.close()


async def test_json_body_and_org(document, settings):
    config = settings.model_copy(
        update={
            "read_only": False,
            "include_operations": "assets_hosts_create",
            "jms_org_id": "org-test",
        }
    )

    def upstream(request):
        assert request.headers["X-JMS-ORG"] == "org-test"
        assert request.headers["Authorization"] == "Bearer upstream-test-token"
        assert json.loads(request.content) == {"name": "test", "address": "10.0.0.1"}
        return httpx.Response(201, json={"id": "test-id"})

    client = JumpServerClient(config, transport=httpx.MockTransport(upstream))
    try:
        result = await client.call(
            compile_catalog(document, config),
            "assets_hosts_create",
            {"body": {"name": "test", "address": "10.0.0.1"}},
        )
        assert not result.isError
    finally:
        await client.close()


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
async def test_status_error_no_body_or_secret_leak(document, settings, status):
    client = JumpServerClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, text="SECRET-UPSTREAM-BODY", headers={"Location": "https://evil.test/"}
            )
        ),
    )
    try:
        result = await client.call(compile_catalog(document, settings), "assets_hosts_list", {})
        assert result.isError
        assert str(status) in result.content[0].text
        assert "SECRET" not in result.content[0].text
    finally:
        await client.close()


async def test_size_cap_and_invalid_input(document, settings):
    config = settings.model_copy(update={"max_response_bytes": 1024})
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 1025)

    client = JumpServerClient(config, transport=httpx.MockTransport(upstream))
    catalog = compile_catalog(document, config)
    try:
        invalid = await client.call(catalog, "assets_hosts_list", {"query": {"limit": "SECRET"}})
        assert invalid.isError and not calls
        assert "SECRET" not in invalid.content[0].text
        assert (await client.call(catalog, "assets_hosts_list", {})).isError
        assert (await client.call(catalog, "disabled", {})).isError
    finally:
        await client.close()


async def test_timeout_no_retry(document, settings):
    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret detail", request=request)

    client = JumpServerClient(settings, transport=httpx.MockTransport(upstream))
    try:
        result = await client.call(compile_catalog(document, settings), "assets_hosts_list", {})
        assert result.isError and "timed out" in result.content[0].text
        assert "secret" not in result.content[0].text and len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "root,path,expected",
    [
        ("https://jms.test", "/api/v1/assets/", "https://jms.test/api/v1/assets/"),
        ("https://jms.test/jms", "/api/v1/assets/", "https://jms.test/jms/api/v1/assets/"),
        ("https://jms.test/jms", "/jms/api/v1/assets/", "https://jms.test/jms/api/v1/assets/"),
    ],
)
def test_deployment_prefix(root, path, expected):
    assert combine_url(root, path) == expected


async def test_encoded_path_and_traversal(document, settings):
    calls = []
    client = JumpServerClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={})
        ),
    )
    try:
        catalog = compile_catalog(document, settings)
        assert (await client.call(catalog, "assets_hosts_read", {"path": {"id": ".."}})).isError
        assert not calls
        result = await client.call(catalog, "assets_hosts_read", {"path": {"id": "abc?x=1"}})
        assert not result.isError
        assert calls[0].url.query == b""
    finally:
        await client.close()
