"""The import of a custom HTTP API (docs/architecture.md, Section 10.3.3, M4.12)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sama.connectors.mcp_remote import MAX_TOOLS
from sama.connectors.openapi import (
    ApiImportError,
    ApiOperation,
    load_document,
    parse_endpoints,
    parse_openapi,
    tool_name,
)

PETS: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1"},
    "servers": [
        {"url": "https://{region}.pets.test/v1/", "variables": {"region": {"default": "eu"}}}
    ],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List the pets.",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
            },
            "post": {
                "operationId": "createPet",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                    },
                },
            },
        },
        "/pets/{petId}": {
            "parameters": [{"$ref": "#/components/parameters/PetId"}],
            "get": {"description": "One pet."},
            "delete": {"operationId": "deletePet"},
        },
    },
    "components": {
        "parameters": {
            "PetId": {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}
        },
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "owner": {"$ref": "#/components/schemas/Owner"},
                },
            },
            "Owner": {
                "type": "object",
                "properties": {
                    "pets": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}
                },
            },
        },
    },
}


def by_name(text: str) -> dict[str, ApiOperation]:
    return {op.tool.name: op for op in parse_openapi(text, max_name=50).operations}


def test_an_openapi_document_gives_one_tool_for_each_operation() -> None:
    imported = parse_openapi(json.dumps(PETS), max_name=50)
    assert imported.base_url == "https://eu.pets.test/v1"
    assert imported.skipped == 0
    ops = {op.tool.name: op for op in imported.operations}
    assert list(ops) == ["list_pets", "create_pet", "get_pets_by_petid", "delete_pet"]
    listed = ops["list_pets"]
    assert (listed.method, listed.path, listed.params, listed.body) == (
        "GET",
        "/pets",
        (("limit", "query"),),
        False,
    )
    # A header parameter is not an argument: sama sets the headers.
    assert set(listed.tool.input_schema["properties"]) == {"limit"}
    assert listed.tool.read_only is True
    assert listed.tool.description == "List the pets. (GET /pets)"
    created = ops["create_pet"]
    assert created.body is True
    assert created.tool.input_schema["required"] == ["body"]
    assert created.tool.read_only is False
    assert created.tool.destructive is False
    # The cyclic $ref stops: the inner Pet is an open schema.
    owner = created.tool.input_schema["properties"]["body"]["properties"]["owner"]
    assert owner["properties"]["pets"]["items"] == {}
    one = ops["get_pets_by_petid"]
    assert one.params == (("petId", "path"),)
    assert one.tool.input_schema["required"] == ["petId"]
    assert ops["delete_pet"].tool.destructive is True


def test_the_stored_operation_comes_back_the_same() -> None:
    for op in parse_openapi(json.dumps(PETS), max_name=50).operations:
        assert ApiOperation.from_stored(json.loads(json.dumps(op.stored()))) == op
    assert ApiOperation.from_stored({"name": "x", "input_schema": {}}) is None


def test_yaml_is_read_and_yaml_aliases_are_refused() -> None:
    text = "openapi: 3.1.0\npaths:\n  /a:\n    get:\n      operationId: getA\n"
    assert [op.tool.name for op in parse_openapi(text, max_name=50).operations] == ["get_a"]
    bomb = 'openapi: 3.0.0\na: &a ["x", "x"]\nb: &b [*a, *a]\npaths: {}\n'
    with pytest.raises(ApiImportError, match="aliases"):
        load_document(bomb)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not: [valid", "not valid JSON or YAML"),
        ("[1, 2]", "not an object"),
        ('{"swagger": "2.0", "paths": {}}', "OpenAPI 3"),
        ('{"openapi": "3.0.0"}', "no paths"),
        ("x" * 2_000_001, "larger than 2 MB"),
    ],
)
def test_a_bad_document_is_refused(text: str, message: str) -> None:
    with pytest.raises(ApiImportError, match=message):
        load_document(text)


def test_a_bad_operation_is_skipped() -> None:
    document = {
        "openapi": "3.0.0",
        "servers": [{"url": "http://plain.test"}],
        "paths": {
            "/ok": {"get": {}},
            "/up/../down": {"get": {}},
            "/items/{id}": {"get": {}},  # The template has no path parameter.
            "/same": {
                "get": {
                    "parameters": [
                        {"name": "x", "in": "query"},
                        {"name": "body", "in": "query"},
                    ]
                }
            },
            "/remote": {"get": {"$ref": "https://evil.test/op.json"}},
        },
    }
    imported = parse_openapi(json.dumps(document), max_name=50)
    assert [op.tool.name for op in imported.operations] == ["get_ok"]
    assert imported.skipped == 3
    # Only an https server is a base URL.
    assert imported.base_url is None


def test_names_are_snake_case_short_and_unique() -> None:
    assert tool_name("getUserByID", "get", "/x", 50) == "get_user_by_id"
    assert tool_name(None, "get", "/users/{id}/posts", 50) == "get_users_by_id_posts"
    assert tool_name("2fa-check", "post", "/x", 50) == "op_2fa_check"
    assert tool_name("a" * 80, "get", "/x", 10) == "a" * 10
    document = {
        "openapi": "3.0.0",
        "paths": {f"/p{i}": {"get": {"operationId": "same"}} for i in range(3)},
    }
    names = [op.tool.name for op in parse_openapi(json.dumps(document), max_name=50).operations]
    assert names == ["same", "same_2", "same_3"]


def test_at_most_max_tools_operations() -> None:
    document = {"openapi": "3.0.0", "paths": {f"/p{i}": {"get": {}} for i in range(MAX_TOOLS + 5)}}
    imported = parse_openapi(json.dumps(document), max_name=50)
    assert len(imported.operations) == MAX_TOOLS
    assert imported.skipped == 5


def test_a_list_of_endpoints_gives_tools_with_path_query_and_body() -> None:
    imported = parse_endpoints(
        [
            {"name": "getItem", "method": "GET", "path": "/items/{id}", "query": ["fields"]},
            {"name": "addItem", "method": "post", "path": "/items", "body": True},
            {"name": "bad", "method": "TRACE", "path": "/x"},
            {"name": "nopath", "method": "GET"},
        ],
        max_name=50,
    )
    ops = {op.tool.name: op for op in imported.operations}
    assert list(ops) == ["get_item", "add_item"]
    assert imported.skipped == 2
    assert ops["get_item"].params == (("id", "path"), ("fields", "query"))
    assert ops["get_item"].tool.input_schema["required"] == ["id"]
    assert ops["add_item"].body is True
    assert ops["add_item"].tool.read_only is False
