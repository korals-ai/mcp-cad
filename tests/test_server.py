"""Tests for the MCP server wiring: the cad_* tools register and map failures
to MCP errors. ezdxf is real; no DWG path exercised (deferred)."""

from __future__ import annotations

import pytest

from src import server


@pytest.mark.asyncio
async def test_cad_tools_are_registered() -> None:
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "cad_read",
        "cad_list_entities",
        "cad_extract_geometry",
        "cad_measure",
        "cad_set_text",
        "cad_rename_layer",
        "cad_create_document",
        "cad_add_layer",
        "cad_add_line",
        "cad_add_polyline",
        "cad_add_circle",
        "cad_add_arc",
        "cad_add_text",
        "cad_add_block_insert",
    } <= names


@pytest.mark.asyncio
async def test_cad_read_describes_src() -> None:
    tools = await server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "cad_read")
    schema = tool.inputSchema
    assert "src" in schema["properties"]
    assert "src" in schema.get("required", [])


def test_cad_read_missing_source_raises() -> None:
    from src.cad_ops import CadError

    with pytest.raises(CadError):
        server.cad_read("/nonexistent/plan.dxf")


def test_cad_create_document_and_add_line_round_trip(tmp_path) -> None:
    dst = str(tmp_path / "plan.dxf")
    created = server.cad_create_document(dst)
    assert created["output"] == dst
    server.cad_add_layer(dst, "WALLS")
    added = server.cad_add_line(dst, [0, 0], [10, 0], layer="WALLS")
    assert added["handle"]
    geo = server.cad_extract_geometry(dst)["geometry"]
    assert geo[0]["layer"] == "WALLS"


def test_cad_add_line_unknown_layer_raises() -> None:
    from src.cad_ops import CadError

    with pytest.raises(CadError):
        server.cad_add_line("/nonexistent/plan.dxf", [0, 0], [1, 1])
