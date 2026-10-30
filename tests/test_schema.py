import copy

import pytest
from jsonschema import Draft202012Validator

from jms_mcp.schema import SchemaError, absolute_path, compile_catalog


def writes(settings, operations):
    return settings.model_copy(update={"read_only": False, "include_operations": operations})


def test_swagger2_default_readonly_and_asset_filter(document, settings):
    catalog = compile_catalog(document, settings)
    assert set(catalog.operations) == {"assets_hosts_list", "assets_hosts_read"}
    assert catalog.operations["assets_hosts_list"].path == "/api/v1/assets/hosts/"
    query = catalog.tools[0].inputSchema["properties"]["query"]["properties"]
    assert query["limit"]["type"] == "integer"
    assert query["is_active"]["type"] == "boolean"
    assert not catalog.skipped


def test_swagger_body_ref_readonly_nullable(document, settings):
    catalog = compile_catalog(document, writes(settings, "assets_hosts_create"))
    tool = catalog.tools[0]
    body = tool.inputSchema["properties"]["body"]
    assert "id" not in body["properties"]
    assert body["required"] == ["name", "address"]
    assert tool.inputSchema["required"] == ["body"]
    Draft202012Validator(tool.inputSchema).validate(
        {"body": {"name": "test", "address": "10.0.0.1", "comment": None}}
    )


@pytest.mark.parametrize(
    "prefix,path,expected",
    [
        ("/api/v1", "/assets/hosts/", "/api/v1/assets/hosts/"),
        ("/api/v1", "/api/v1/assets/hosts/", "/api/v1/assets/hosts/"),
        ("/", "/api/v1/assets/hosts/", "/api/v1/assets/hosts/"),
    ],
)
def test_path_prefix(prefix, path, expected):
    assert absolute_path(path, prefix) == expected


@pytest.mark.parametrize(
    "path", ["https://evil.test/", "//evil.test/", "/../evil", "/%2e%2e/evil", "/assets/?x=1"]
)
def test_path_rejects_unsafe_routes(path):
    with pytest.raises(SchemaError):
        absolute_path(path, "/api/v1")


def test_path_level_parameter(document, settings):
    tool = compile_catalog(document, settings).operations["assets_hosts_read"].tool
    assert tool.inputSchema["properties"]["path"]["required"] == ["id"]


def test_parameter_override(document, settings):
    op = document["paths"]["/assets/hosts/{id}/"]["get"]
    op["parameters"] = [{"name": "id", "in": "path", "type": "integer"}]
    tool = compile_catalog(document, settings).operations["assets_hosts_read"].tool
    assert tool.inputSchema["properties"]["path"]["properties"]["id"]["type"] == "integer"


def test_write_mode_requires_explicit_allowlist(document, settings):
    with pytest.raises(SchemaError, match="allowlist"):
        compile_catalog(document, writes(settings, ""))


def test_tool_cap_is_error_not_truncation(document, settings):
    with pytest.raises(SchemaError, match="exceeds max_tools"):
        compile_catalog(document, settings.model_copy(update={"max_tools": 1}))


def test_exact_allowlist_and_exclusion(document, settings):
    config = settings.model_copy(
        update={
            "include_operations": "assets_hosts_list,assets_hosts_read",
            "exclude_operations": "assets_hosts_read",
        }
    )
    assert list(compile_catalog(document, config).operations) == ["assets_hosts_list"]


def test_openapi3_body_and_server_host_ignored(settings):
    doc = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://untrusted.test/api/v1"}],
        "paths": {
            "/assets/hosts/": {
                "post": {
                    "operationId": "create_host",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        },
                    },
                }
            }
        },
    }
    tool = compile_catalog(doc, writes(settings, "create_host")).tools[0]
    assert tool.inputSchema["properties"]["body"]["required"] == ["name"]


@pytest.mark.parametrize(
    "kind",
    [
        "remote_ref",
        "recursive_ref",
        "form",
        "cookie",
        "header",
        "missing_path",
        "non_json",
        "file",
        "object_query",
    ],
)
def test_unsupported_operations_reported(document, settings, kind):
    # Keep one valid GET so the complete catalog remains usable.
    bad = copy.deepcopy(document["paths"]["/assets/hosts/"]["post"])
    bad["operationId"] = "bad_operation"
    body = bad["parameters"][0]
    if kind == "remote_ref":
        body["schema"] = {"$ref": "https://evil.test/schema.json"}
    elif kind == "recursive_ref":
        document["definitions"]["Loop"] = {
            "type": "object",
            "properties": {"next": {"$ref": "#/definitions/Loop"}},
        }
        body["schema"] = {"$ref": "#/definitions/Loop"}
    elif kind in {"form", "cookie", "header"}:
        bad["parameters"] = [
            {
                "name": "x",
                "in": {"form": "formData", "cookie": "cookie", "header": "header"}[kind],
                "required": True,
                "type": "string",
            }
        ]
    elif kind == "missing_path":
        bad["parameters"].append({"name": "id", "in": "path", "type": "string"})
    elif kind == "non_json":
        bad["consumes"] = ["application/xml"]
    elif kind == "file":
        body["schema"] = {"type": "file"}
    elif kind == "object_query":
        bad["parameters"].append({"name": "filter", "in": "query", "type": "object"})
    document["paths"]["/assets/hosts/"]["post"] = bad
    catalog = compile_catalog(document, writes(settings, "assets_hosts_list,bad_operation"))
    assert list(catalog.operations) == ["assets_hosts_list"]
    assert catalog.skipped[0]["operation"] == "bad_operation"


def test_duplicate_names_fail(document, settings):
    document["paths"]["/assets/hosts/{id}/"]["get"]["operationId"] = "assets_hosts_list"
    with pytest.raises(SchemaError, match="Duplicate"):
        compile_catalog(document, settings)


def test_no_mutation(document, settings):
    before = copy.deepcopy(document)
    compile_catalog(document, settings)
    assert before == document


def test_tag_filter(document, settings):
    config = settings.model_copy(update={"include_tags": "absent_tag"})
    with pytest.raises(SchemaError, match="No supported tools"):
        compile_catalog(document, config)
