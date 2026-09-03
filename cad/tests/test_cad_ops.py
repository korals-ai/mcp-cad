"""Tests for the DXF read + edit logic. ezdxf is a real dep (pip-installed in
the pre_build gate), so these build actual DXF docs and round-trip them."""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

from src.cad_ops import (
    CadError,
    add_arc,
    add_block_insert,
    add_circle,
    add_layer,
    add_line,
    add_polyline,
    add_text,
    create_document,
    extract_geometry,
    list_entities,
    measure,
    read_summary,
    rename_layer,
    set_text,
)


def _make_dxf(path: Path) -> None:
    doc = ezdxf.new()
    doc.layers.add("CAMERAS")
    msp = doc.modelspace()
    msp.add_text("CAM-01", dxfattribs={"layer": "CAMERAS"})
    msp.add_line((0, 0), (10, 0))
    doc.saveas(str(path))


def _make_rich_dxf(path: Path) -> None:
    """A plan with the entity variety the read/extract ops target: text, a
    wall line + closed room polyline, a circle, an arc, and a placed camera
    block (INSERT)."""
    doc = ezdxf.new()
    doc.layers.add("CAMERAS")
    doc.layers.add("WALLS")
    cam = doc.blocks.new(name="CAMERA")
    cam.add_circle((0, 0), radius=1)
    msp = doc.modelspace()
    msp.add_text("CAM-01", dxfattribs={"layer": "CAMERAS"})
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "WALLS"})
    msp.add_lwpolyline(
        [(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": "WALLS"}
    )
    msp.add_circle((5, 5), radius=2, dxfattribs={"layer": "CAMERAS"})
    msp.add_arc((5, 5), radius=3, start_angle=0, end_angle=90, dxfattribs={"layer": "CAMERAS"})
    msp.add_blockref("CAMERA", (3, 3), dxfattribs={"layer": "CAMERAS"})
    doc.saveas(str(path))


def test_read_summary_extracts_layers_text_counts(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_dxf(src)
    s = read_summary(src)
    assert "CAMERAS" in s["layers"]
    assert "CAM-01" in s["text"]
    assert s["entity_counts"].get("TEXT") == 1
    assert s["entity_counts"].get("LINE") == 1
    assert s["dxf_version"]


def test_list_entities_inventories_types_text_and_blocks(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_rich_dxf(src)
    res = list_entities(src)
    assert res["count"] == len(res["entities"])
    by_type = {row["type"] for row in res["entities"]}
    assert {"TEXT", "LINE", "LWPOLYLINE", "CIRCLE", "ARC", "INSERT"} <= by_type
    text_row = next(r for r in res["entities"] if r["type"] == "TEXT")
    assert text_row["text"] == "CAM-01"
    insert_row = next(r for r in res["entities"] if r["type"] == "INSERT")
    assert insert_row["block"] == "CAMERA"
    assert all("layer" in r and "handle" in r for r in res["entities"])


def test_list_entities_filters_by_kind(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_rich_dxf(src)
    res = list_entities(src, kind="line")  # case-insensitive
    assert res["count"] == 1
    assert res["entities"][0]["type"] == "LINE"
    assert res["entities"][0]["layer"] == "WALLS"


def test_extract_geometry_lengths_and_coords(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_rich_dxf(src)
    res = extract_geometry(src)
    geo = {row["type"]: row for row in res["geometry"]}
    # a straight 10-unit wall
    assert geo["LINE"]["length"] == pytest.approx(10.0)
    assert geo["LINE"]["start"] == [0.0, 0.0, 0.0]
    # a closed 10x10 room => perimeter 40
    assert geo["LWPOLYLINE"]["closed"] is True
    assert geo["LWPOLYLINE"]["length"] == pytest.approx(40.0)
    # circle/arc carry center+radius but no length
    assert geo["CIRCLE"]["radius"] == pytest.approx(2.0)
    assert geo["ARC"]["start_angle"] == pytest.approx(0.0)
    # total covers line + polyline only (10 + 40); text/insert/circle excluded
    assert res["total_line_length"] == pytest.approx(50.0)


def test_measure_extents_counts_and_layers(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_rich_dxf(src)
    res = measure(src)
    assert res["entity_counts"].get("LWPOLYLINE") == 1
    assert res["entities_per_layer"].get("WALLS") == 2  # line + polyline
    assert {"CAMERAS", "WALLS"} <= set(res["layers"])
    assert res["extents"] is not None
    # extents must at least contain the 10x10 room (other entities — arc,
    # circle, camera block — may push the bounds out further, so assert
    # containment, not an exact size).
    ext = res["extents"]
    assert ext["min"][0] <= 0.0 and ext["min"][1] <= 0.0
    assert ext["max"][0] >= 10.0 and ext["max"][1] >= 10.0
    assert ext["size"][0] >= 10.0 and ext["size"][1] >= 10.0


def test_measure_empty_drawing_has_null_extents(tmp_path: Path) -> None:
    src = tmp_path / "empty.dxf"
    ezdxf.new().saveas(str(src))
    res = measure(src)
    assert res["extents"] is None
    assert res["entity_counts"] == {}


def test_dwg_is_rejected_with_clear_message(tmp_path: Path) -> None:
    src = tmp_path / "plan.dwg"
    src.write_bytes(b"not really a dwg")
    with pytest.raises(CadError, match="DWG is not supported"):
        read_summary(src)


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(CadError, match="does not exist"):
        read_summary(tmp_path / "nope.dxf")


def test_invalid_dxf_raises(tmp_path: Path) -> None:
    src = tmp_path / "bad.dxf"
    src.write_text("this is not a dxf")
    with pytest.raises(CadError):
        read_summary(src)


def test_set_text_replaces_and_writes_edited_copy(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_dxf(src)
    res = set_text(src, "CAM-01", "CAM-99")
    out = Path(res["output"])
    assert res["replaced"] == 1
    assert out == tmp_path / "plan.edited.dxf"
    assert src.exists()  # original untouched
    assert "CAM-99" in read_summary(out)["text"]
    assert "CAM-01" not in read_summary(out)["text"]


def test_set_text_no_match_raises(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_dxf(src)
    with pytest.raises(CadError, match="nothing changed"):
        set_text(src, "NOPE", "X")


def test_rename_layer(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_dxf(src)
    res = rename_layer(src, "CAMERAS", "CCTV")
    layers = read_summary(Path(res["output"]))["layers"]
    assert "CCTV" in layers
    assert "CAMERAS" not in layers


def test_rename_missing_layer_raises(tmp_path: Path) -> None:
    src = tmp_path / "plan.dxf"
    _make_dxf(src)
    with pytest.raises(CadError, match="not found"):
        rename_layer(src, "GHOST", "X")


def test_oversized_file_rejected_before_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The byte guard must fire on st_size BEFORE ezdxf parses anything —
    garbage bytes over the cap must produce the size error, never the
    "not a valid DXF" parse error (proving no parse was attempted)."""
    monkeypatch.setattr("src.cad_ops.MAX_DXF_BYTES", 64)
    src = tmp_path / "huge.dxf"
    src.write_bytes(b"x" * 128)
    with pytest.raises(CadError, match="over the 0 MB limit"):
        read_summary(src)


def test_oversized_error_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.cad_ops.MAX_DXF_BYTES", 64)
    src = tmp_path / "huge.dxf"
    src.write_bytes(b"x" * 128)
    with pytest.raises(CadError) as exc:
        measure(src)
    msg = str(exc.value)
    assert "huge.dxf" in msg
    assert "smaller DXF" in msg  # tells the agent what to do, not just "no"


def test_entity_ceiling_rejected_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small-on-disk but dense drawing (the incident shape: ~202k
    stroke-traced LINEs) is rejected on modelspace entity count."""
    monkeypatch.setattr("src.cad_ops.MAX_DXF_ENTITIES", 3)
    src = tmp_path / "dense.dxf"
    doc = ezdxf.new()
    msp = doc.modelspace()
    for i in range(5):
        msp.add_line((i, 0), (i, 1))
    doc.saveas(str(src))
    with pytest.raises(CadError, match="5 modelspace entities"):
        list_entities(src)


def test_size_guards_pass_normal_files(tmp_path: Path) -> None:
    """Default caps must not touch a normal plan — the guard exists for the
    pathological case, not everyday tender drawings."""
    src = tmp_path / "plan.dxf"
    _make_rich_dxf(src)
    assert read_summary(src)["entity_counts"]


# --- Entity creation (authoring) --------------------------------------------


def test_create_document_writes_empty_valid_dxf(tmp_path: Path) -> None:
    dst = tmp_path / "job" / "plan.dxf"
    res = create_document(dst)
    assert res["output"] == str(dst)
    assert dst.exists()
    assert read_summary(dst)["entity_counts"] == {}


def test_create_document_refuses_to_clobber_by_default(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="already exists"):
        create_document(dst)


def test_create_document_overwrite_true_replaces(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_layer(dst, "WALLS")
    create_document(dst, overwrite=True)
    assert "WALLS" not in read_summary(dst)["layers"]


def test_add_layer_then_add_line_round_trips(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_layer(dst, "WALLS")
    res = add_line(dst, (0, 0), (10, 0), layer="WALLS")
    assert res["output"] == str(dst)
    assert res["handle"]
    geo = extract_geometry(dst)["geometry"]
    assert geo[0]["type"] == "LINE"
    assert geo[0]["layer"] == "WALLS"
    assert geo[0]["length"] == pytest.approx(10.0)


def test_add_layer_duplicate_raises(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_layer(dst, "WALLS")
    with pytest.raises(CadError, match="already exists"):
        add_layer(dst, "WALLS")


def test_add_line_unknown_layer_raises_actionable_error(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="call add_layer"):
        add_line(dst, (0, 0), (1, 1), layer="GHOST")


def test_add_line_default_layer_zero_needs_no_setup(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    res = add_line(dst, (0, 0), (1, 1))  # layer="0" — always present
    assert res["handle"]


def test_add_polyline_round_trips_and_closes(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_polyline(dst, [(0, 0), (10, 0), (10, 10), (0, 10)], closed=True)
    geo = extract_geometry(dst)["geometry"][0]
    assert geo["closed"] is True
    assert geo["length"] == pytest.approx(40.0)


def test_add_polyline_too_few_points_raises(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="at least 2 points"):
        add_polyline(dst, [(0, 0)])


def test_add_circle_round_trips(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_circle(dst, (5, 5), radius=2)
    geo = extract_geometry(dst)["geometry"][0]
    assert geo["type"] == "CIRCLE"
    assert geo["radius"] == pytest.approx(2.0)


def test_add_circle_non_positive_radius_raises(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="radius must be positive"):
        add_circle(dst, (0, 0), radius=0)


def test_add_arc_round_trips(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_arc(dst, (5, 5), radius=3, start_angle=0, end_angle=90)
    geo = extract_geometry(dst)["geometry"][0]
    assert geo["type"] == "ARC"
    assert geo["start_angle"] == pytest.approx(0.0)
    assert geo["end_angle"] == pytest.approx(90.0)


def test_add_arc_non_positive_radius_raises(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="radius must be positive"):
        add_arc(dst, (0, 0), radius=-1, start_angle=0, end_angle=90)


def test_add_text_round_trips(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_text(dst, "CAM-01", (1, 2))
    assert "CAM-01" in read_summary(dst)["text"]


def test_add_text_non_positive_height_raises(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="height must be positive"):
        add_text(dst, "X", (0, 0), height=0)


def test_add_block_insert_undefined_block_raises_actionable_error(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    with pytest.raises(CadError, match="not defined"):
        add_block_insert(dst, "CAMERA", (0, 0))


def test_add_block_insert_places_defined_block(tmp_path: Path) -> None:
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    # Simulate a template DXF that already carries a symbol block, the way a
    # user-supplied template would — cad_ops has no block-definition op, so
    # the test builds one directly with ezdxf, matching add_block_insert's
    # documented precondition.
    doc = ezdxf.readfile(str(dst))
    blk = doc.blocks.new(name="CAMERA")
    blk.add_circle((0, 0), radius=1)
    doc.saveas(str(dst))

    res = add_block_insert(dst, "CAMERA", (3, 3), scale=2.0, rotation=45.0)
    assert res["handle"]
    entities = list_entities(dst, kind="INSERT")["entities"]
    assert entities[0]["block"] == "CAMERA"


def test_add_entities_reuse_the_same_size_guard_as_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add_* call goes through _load() like a read does, so a runaway
    authoring sequence hits the same entity-count ceiling — the incident's
    Prevention item 3 (self-checked, bounded output) for free."""
    dst = tmp_path / "plan.dxf"
    create_document(dst)
    add_line(dst, (0, 0), (1, 0))
    # After the first line, the doc already holds 1 entity — cap at 0 so the
    # NEXT add_* call's _load() sees 1 > 0 and rejects before adding a second.
    monkeypatch.setattr("src.cad_ops.MAX_DXF_ENTITIES", 0)
    with pytest.raises(CadError, match="modelspace entities"):
        add_line(dst, (1, 0), (2, 0))
