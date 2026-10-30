"""Compile Swagger 2.0 and OpenAPI 3 operations into bounded MCP catalogs.

No remote references are fetched. Unsupported operations are reported, never
silently represented as tools with missing input fields.
"""

import copy
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError
from mcp.types import Tool, ToolAnnotations

from .config import Settings


class SchemaError(ValueError):
    """Invalid or unsupported API schema."""


def resolve_ref(doc: dict, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise SchemaError("Only local JSON references are supported")
    node: Any = doc
    try:
        for key in unquote(ref[2:]).split("/"):
            node = node[key.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError) as exc:
        raise SchemaError("Unresolved local JSON reference") from exc
    return node


def dereference(node: dict, doc: dict, stack: tuple[str, ...] = ()) -> dict:
    if "$ref" not in node:
        return node
    ref = node["$ref"]
    if ref in stack or len(stack) > 40:
        raise SchemaError("Recursive or deeply nested reference is unsupported")
    target = resolve_ref(doc, ref)
    if not isinstance(target, dict):
        raise SchemaError("Reference must target an object")
    return {
        **dereference(target, doc, (*stack, ref)),
        **{k: v for k, v in node.items() if k != "$ref"},
    }


def input_schema(node: Any, doc: dict, stack: tuple[str, ...] = (), depth: int = 0) -> dict:
    if depth > 40 or not isinstance(node, dict):
        raise SchemaError("Invalid or deeply nested input schema")
    if "$ref" in node:
        ref = node["$ref"]
        if ref in stack:
            raise SchemaError("Recursive request schema is unsupported")
        merged = dereference(node, doc)
        return input_schema(merged, doc, (*stack, ref), depth + 1)
    if node.get("type") == "file" or node.get("format") == "binary":
        raise SchemaError("File uploads are unsupported")
    # Copy only JSON Schema keywords, avoiding arbitrary vendor annotations.
    allowed = {
        "type",
        "title",
        "description",
        "enum",
        "const",
        "default",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
    result = {k: copy.deepcopy(v) for k, v in node.items() if k in allowed and v is not None}
    # Swagger 2/OpenAPI 3.0 use boolean exclusive bounds.
    for bound in ("Minimum", "Maximum"):
        key, plain = f"exclusive{bound}", bound.lower()
        if isinstance(result.get(key), bool):
            exclusive = result.pop(key)
            if exclusive and plain in result:
                result[key] = result.pop(plain)
    if "properties" in node:
        props = {}
        for name, value in node["properties"].items():
            expanded = dereference(value, doc)
            if not expanded.get("readOnly", False):
                props[name] = input_schema(value, doc, stack, depth + 1)
        result["properties"] = props
        if "required" in node:
            result["required"] = [k for k in node["required"] if k in props]
    elif isinstance(node.get("required"), list):
        result["required"] = node["required"]
    for key in ("items", "additionalProperties", "not"):
        if key in node:
            value = node[key]
            result[key] = (
                value if isinstance(value, bool) else input_schema(value, doc, stack, depth + 1)
            )
    for key in ("allOf", "anyOf", "oneOf"):
        if key in node:
            result[key] = [input_schema(v, doc, stack, depth + 1) for v in node[key]]
    if node.get("nullable") or node.get("x-nullable"):
        result = {"anyOf": [result, {"type": "null"}]}
    return result


def absolute_path(path: str, prefix: str) -> str:
    for value in (path, prefix):
        parsed = urlsplit(value)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not value.startswith("/")
            or "//" in value
            or any(p in {".", ".."} for p in unquote(value).split("/"))
        ):
            raise SchemaError("API paths must be absolute local paths without traversal")
    prefix = prefix.rstrip("/")
    if not prefix or path == prefix or path.startswith(prefix + "/"):
        return path
    return prefix + path


def csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass
class Operation:
    tool: Tool
    method: str
    path: str
    parameters: list[dict]
    operation_id: str


@dataclass
class Catalog:
    operations: dict[str, Operation] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    filtered: int = 0

    @property
    def tools(self) -> list[Tool]:
        return [operation.tool for operation in self.operations.values()]


def compile_operation(
    doc: dict,
    path: str,
    method: str,
    operation: dict,
    common_params: list[dict],
    settings: Settings,
) -> Operation:
    op_id = operation.get("operationId") or f"{method}_{path}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", op_id).strip("_")
    if not name or len(name) > 64:
        raise SchemaError("Tool name must have 1..64 characters")
    parameters = {}
    for item in [*common_params, *operation.get("parameters", [])]:
        param = dict(dereference(item, doc))
        if doc.get("swagger") == "2.0" and param.get("type") == "array":
            param.setdefault("collectionFormat", "csv")
        parameters[(param["in"], param["name"])] = param
    props: dict[str, Any] = {}
    required = []
    for location in ("path", "query"):
        group_props, group_required = {}, []
        for (where, param_name), param in parameters.items():
            if where != location:
                continue
            style = param.get("style", "form" if location == "query" else "simple")
            if style != ("form" if location == "query" else "simple"):
                raise SchemaError("Unsupported parameter style")
            if location == "query" and param.get("explode") is False:
                # Scalar explode is immaterial; arrays/objects require explicit support.
                param_type = dereference(param.get("schema", param), doc).get("type")
                if param_type in {"array", "object"}:
                    raise SchemaError("Non-exploded query collections are unsupported")
            value = input_schema(param.get("schema", param), doc)
            value_type = dereference(param.get("schema", param), doc).get("type")
            if value_type not in {"string", "integer", "number", "boolean", "array"}:
                raise SchemaError("Only primitive and array query/path parameters are supported")
            if value_type == "array":
                item_type = (
                    dereference(param.get("schema", param), doc).get("items", {}).get("type")
                )
                if item_type not in {"string", "integer", "number", "boolean"}:
                    raise SchemaError("Only primitive array parameters are supported")
            if value_type == "object" or (location == "path" and value_type == "array"):
                raise SchemaError("Object parameters and path arrays are unsupported")
            if value_type == "array" and param.get("collectionFormat", "multi") not in {
                "multi",
                "csv",
                "ssv",
                "pipes",
                "tsv",
            }:
                raise SchemaError("Unsupported query collectionFormat")
            if param.get("description"):
                value["description"] = param["description"]
            group_props[param_name] = value
            if location == "path" or param.get("required"):
                group_required.append(param_name)
        if group_props:
            props[location] = {
                "type": "object",
                "properties": group_props,
                "additionalProperties": False,
            }
            if group_required:
                props[location]["required"] = group_required
                required.append(location)
    body_params = [p for p in parameters.values() if p["in"] == "body"]
    if any(p["in"] in {"formData", "cookie"} for p in parameters.values()):
        raise SchemaError("Form, file and cookie parameters are unsupported")
    for p in parameters.values():
        if p["in"] == "header" and p.get("required"):
            managed = {"authorization", "accept", "content-type"}
            if settings.jms_org_id:
                managed.add("x-jms-org")
            if p["name"].lower() not in managed:
                raise SchemaError("Required unmanaged header is unsupported")
    body_schema, body_required = None, False
    if body_params:
        if len(body_params) != 1:
            raise SchemaError("Multiple Swagger body parameters")
        consumes = operation.get("consumes", doc.get("consumes", ["application/json"]))
        if "application/json" not in consumes:
            raise SchemaError("Only JSON request bodies are supported")
        body_schema = input_schema(body_params[0]["schema"], doc)
        body_required = body_params[0].get("required", False)
    elif "requestBody" in operation:
        request_body = dereference(operation["requestBody"], doc)
        content = request_body.get("content", {})
        if "application/json" not in content:
            raise SchemaError("Only JSON request bodies are supported")
        body_schema = input_schema(content["application/json"].get("schema", {}), doc)
        body_required = request_body.get("required", False)
    if body_schema is not None:
        if method in {"get", "delete"}:
            raise SchemaError("GET/DELETE request bodies are unsupported")
        props["body"] = body_schema
        if body_required:
            required.append("body")
    expected = set(re.findall(r"{([^{}]+)}", path))
    actual = set(props.get("path", {}).get("properties", {}))
    if expected != actual:
        raise SchemaError("Path placeholders do not match path parameters")
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    try:
        Draft202012Validator.check_schema(schema)
    except JsonSchemaError as exc:
        raise SchemaError("Invalid generated input schema") from exc
    summary = operation.get("summary") or op_id
    description = f"{summary}\n{method.upper()} {path}\n"
    description += (operation.get("description") or "")[:1000]
    tool = Tool(
        name=name,
        description=description,
        inputSchema=schema,
        annotations=ToolAnnotations(
            readOnlyHint=method == "get",
            destructiveHint=method != "get",
            idempotentHint=method in {"get", "put", "delete"},
            openWorldHint=True,
        ),
    )
    return Operation(tool, method, path, list(parameters.values()), op_id)


def compile_catalog(doc: dict, settings: Settings) -> Catalog:
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        raise SchemaError("API schema must contain a paths object")
    if doc.get("swagger") == "2.0":
        prefix = doc.get("basePath") or "/"
    elif str(doc.get("openapi", "")).startswith("3."):
        servers = doc.get("servers") or [{}]
        prefix = urlsplit(servers[0].get("url", "/")).path or "/"
        if "{" in prefix:
            raise SchemaError("Templated server paths are unsupported")
    else:
        raise SchemaError("Expected Swagger 2.0 or OpenAPI 3.x")
    if not settings.read_only and not csv(settings.include_operations):
        raise SchemaError("Write mode requires an explicit include_operations allowlist")
    catalog = Catalog()
    for raw_path, raw_item in doc["paths"].items():
        path = absolute_path(raw_path, prefix)
        item = dereference(raw_item, doc)
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            op_id = operation.get("operationId", f"{method}_{path}")
            allowed_paths = csv(settings.include_paths)
            included_ops, excluded_ops = (
                csv(settings.include_operations),
                csv(settings.exclude_operations),
            )
            if (
                (settings.read_only and method != "get")
                or (allowed_paths and not any(fnmatch.fnmatchcase(path, p) for p in allowed_paths))
                or (included_ops and op_id not in included_ops)
                or op_id in excluded_ops
                or (
                    csv(settings.include_tags)
                    and not set(operation.get("tags", [])).intersection(csv(settings.include_tags))
                )
            ):
                catalog.filtered += 1
                continue
            try:
                compiled = compile_operation(
                    doc, path, method, operation, item.get("parameters", []), settings
                )
            except (SchemaError, KeyError, TypeError, ValueError) as exc:
                catalog.skipped.append({"operation": op_id, "reason": str(exc)[:200]})
                continue
            if compiled.tool.name in catalog.operations:
                raise SchemaError(f"Duplicate MCP tool name: {compiled.tool.name}")
            catalog.operations[compiled.tool.name] = compiled
    if len(catalog.operations) > settings.max_tools:
        raise SchemaError(
            f"Catalog has {len(catalog.operations)} tools, exceeds max_tools="
            f"{settings.max_tools}; narrow include_paths/include_operations"
        )
    if not catalog.operations:
        detail = "; ".join(f"{v['operation']}: {v['reason']}" for v in catalog.skipped[:3])
        raise SchemaError("No supported tools remain; inspect schema and filters. " + detail)
    return catalog
