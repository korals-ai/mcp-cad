"""Read, edit, and author CAD drawings via ezdxf (DXF only, this phase).

CCTV tenders carry floor plans and camera-placement diagrams. This sidecar
lets the agent read their content (text labels, layers, dimensions, geometry
counts), make minor edits to an existing DXF (set/replace text, rename a
layer), and — since 2026-08-04 — author a new DXF from scratch, one entity at
a time (``create_document`` + ``add_*``). The latter backs AI-vision
PDF-to-CAD reconstruction: the agent visually reads a rasterized PDF page
(``office`` sidecar's ``pdf_to_images``) and reconstructs it as real DXF
entities via these ops, rather than running an automated vectorizer that
traces every pixel/glyph — see the "Entity creation" section below and
docs/incidents/2026-08-04-pdf-to-dxf-oom-and-viewer-freeze.md for why that
distinction matters.

Scope this phase: **DXF only.** DWG read (via a from-source LibreDWG dwg2dxf
build) is a deferred follow-up — see docs/plan §6f. A .dwg passed here is
rejected with an actionable error rather than silently mis-handled.

ezdxf is MIT, pure-Python-ish, and does true read-modify-write on DXF
R12-R2018. Importable contract:

    from pathlib import Path
    from src.cad_ops import (
        read_summary, list_entities, extract_geometry, measure,
        set_text, rename_layer,
        create_document, add_layer, add_line, add_polyline, add_circle,
        add_arc, add_text, add_block_insert,
        CadError,
    )
"""

from __future__ import annotations

import logging
from itertools import pairwise
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.lldxf.const import DXFError

logger = logging.getLogger(__name__)


class CadError(RuntimeError):
    """A CAD op failed: unsupported format (e.g. DWG this phase), missing
    source, unreadable/corrupt DXF, an oversized source, or an edit that
    matched nothing."""


# ezdxf.readfile() materialises the WHOLE file as an in-memory object graph —
# roughly 5-20x the on-disk bytes, scaling with entity count more than bytes —
# and this server runs under a hard container memory limit (see the sidecar
# roster in the workspace-operator chart values). An unbounded parse doesn't
# fail cleanly: it OOM-kills the whole MCP server mid-call, taking every other
# in-flight CAD op with it (2026-08-04 incident: a 36.5 MB DXF with ~202k
# stroke-traced LINE entities — see
# docs/incidents/2026-08-04-pdf-to-dxf-oom-and-viewer-freeze.md).
# So reject BEFORE parsing on bytes (st_size is free), and after opening on
# entity count (bounds the object graph the per-entity ops then walk into
# giant JSON). Honest limit: the entity ceiling runs after the parse, so a
# sub-cap-bytes but hyper-dense file can still OOM the parse itself — full
# containment is the tracked subprocess+RLIMIT_AS follow-up (docs/todo.md).
# Raising the container memLimit instead is NOT a fix — it just moves the OOM
# to the next larger/denser file.
MAX_DXF_BYTES = 15 * 1024 * 1024
MAX_DXF_ENTITIES = 150_000


def _load(src: Path) -> Any:
    """Open a DXF document, mapping ezdxf failures to CadError. Rejects DWG
    early with a clear message (no DWG support until the LibreDWG phase), and
    rejects oversized sources (bytes before parse, entity count after) so a
    huge file is an actionable error instead of an OOM-killed server."""
    if not src.exists():
        raise CadError(f"source does not exist: {src}")
    if not src.is_file():
        raise CadError(f"source is not a file: {src}")
    if src.suffix.lower() == ".dwg":
        raise CadError(
            "DWG is not supported yet — only DXF. Convert the DWG to DXF first, "
            "or wait for the DWG phase (LibreDWG). See docs/plan toolspace §6f."
        )
    size = src.stat().st_size
    if size > MAX_DXF_BYTES:
        raise CadError(
            f"{src.name} is {size / 1_048_576:.1f} MB — over the "
            f"{MAX_DXF_BYTES // 1_048_576} MB limit this CAD tool can parse "
            "in-memory (a larger parse would crash the tool for every caller). "
            "Produce a smaller DXF instead: export a cropped area or fewer "
            "layers, purge unused entities, or keep text as TEXT/MTEXT rather "
            "than stroke-traced outlines."
        )
    try:
        doc = ezdxf.readfile(str(src))
    except OSError as exc:
        raise CadError(f"cannot read {src.name}: {exc}") from exc
    except DXFError as exc:
        raise CadError(f"not a valid DXF ({src.name}): {exc}") from exc
    entity_count = len(doc.modelspace())
    if entity_count > MAX_DXF_ENTITIES:
        raise CadError(
            f"{src.name} has {entity_count} modelspace entities — over the "
            f"{MAX_DXF_ENTITIES} this CAD tool can handle (memory scales with "
            "entity count; a denser drawing would crash the tool). Reduce the "
            "drawing: split it, drop non-essential layers, or regenerate it "
            "with real TEXT entities instead of per-glyph stroke outlines."
        )
    return doc


def read_summary(src: Path) -> dict[str, Any]:
    """Extract a structured summary of a DXF: version, layers, a per-type
    entity count in modelspace, and the text strings (TEXT/MTEXT) — the
    content an agent reasons about on a tender plan.

    Returns a JSON-serialisable dict. Raises ``CadError`` on any failure.
    """
    doc = _load(src)
    msp = doc.modelspace()

    counts: dict[str, int] = {}
    texts: list[str] = []
    for e in msp:
        dxftype = e.dxftype()
        counts[dxftype] = counts.get(dxftype, 0) + 1
        if dxftype == "TEXT":
            texts.append(e.dxf.text)
        elif dxftype == "MTEXT":
            # MTEXT.text includes inline formatting codes; plain_text() strips them.
            texts.append(e.plain_text())

    return {
        "dxf_version": doc.dxfversion,
        "layers": sorted(layer.dxf.name for layer in doc.layers),
        "entity_counts": counts,
        "text": texts,
    }


def list_entities(src: Path, kind: str | None = None) -> dict[str, Any]:
    """Enumerate modelspace entities, optionally filtered to one DXF type
    (e.g. ``"LINE"``, ``"INSERT"``, ``"MTEXT"`` — case-insensitive).

    Each row carries the fields an agent reasons about — type, layer, handle,
    and the text for TEXT/MTEXT — without needing ezdxf itself. Returns
    ``{"count", "entities": [...]}``. Raises ``CadError`` on read failure.
    """
    doc = _load(src)
    msp = doc.modelspace()
    want = kind.upper() if kind else None
    rows: list[dict[str, Any]] = []
    for e in msp:
        t = e.dxftype()
        if want is not None and t != want:
            continue
        row: dict[str, Any] = {"type": t, "layer": e.dxf.layer, "handle": e.dxf.handle}
        if t == "TEXT":
            row["text"] = e.dxf.text
        elif t == "MTEXT":
            row["text"] = e.plain_text()
        elif t == "INSERT":
            row["block"] = e.dxf.name  # block reference — a placed symbol (e.g. a camera)
        rows.append(row)
    return {"count": len(rows), "entities": rows}


def _polyline_length(points: list[tuple[float, float]], *, closed: bool) -> float:
    """Sum the 2D segment lengths of an (optionally closed) polyline."""
    total = 0.0
    for (ax, ay), (bx, by) in pairwise(points):
        total += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    if closed and len(points) > 2:
        (ax, ay), (bx, by) = points[-1], points[0]
        total += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    return total


def extract_geometry(src: Path) -> dict[str, Any]:
    """Extract coordinates + lengths for the geometric primitives on a plan
    (LINE, LWPOLYLINE, CIRCLE, ARC) — the raw geometry behind walls, room
    outlines and camera cones. Non-geometric entities (text, block refs) are
    skipped; use ``list_entities``/``read_summary`` for those.

    Returns ``{"count", "total_line_length", "geometry": [...]}`` with one row
    per primitive. Raises ``CadError`` on read failure.
    """
    doc = _load(src)
    msp = doc.modelspace()
    rows: list[dict[str, Any]] = []
    total_length = 0.0
    for e in msp:
        t = e.dxftype()
        row: dict[str, Any] = {"type": t, "layer": e.dxf.layer, "handle": e.dxf.handle}
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            row["start"] = [s.x, s.y, s.z]
            row["end"] = [en.x, en.y, en.z]
            length = (en - s).magnitude
            row["length"] = length
            total_length += length
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            row["points"] = [[x, y] for x, y in pts]
            row["closed"] = bool(e.closed)
            length = _polyline_length(pts, closed=bool(e.closed))
            row["length"] = length
            total_length += length
        elif t == "CIRCLE":
            c = e.dxf.center
            row["center"] = [c.x, c.y, c.z]
            row["radius"] = e.dxf.radius
        elif t == "ARC":
            c = e.dxf.center
            row["center"] = [c.x, c.y, c.z]
            row["radius"] = e.dxf.radius
            row["start_angle"] = e.dxf.start_angle
            row["end_angle"] = e.dxf.end_angle
        else:
            continue
        rows.append(row)
    return {"count": len(rows), "total_line_length": total_length, "geometry": rows}


def measure(src: Path) -> dict[str, Any]:
    """Summarise a drawing's size and composition: the overall bounding box
    (extents + size), per-type entity counts, per-layer entity counts, and the
    layer list. The one-call "how big / what's in this plan" overview.

    Returns a JSON-serialisable dict; ``extents`` is ``None`` for an empty
    drawing. Raises ``CadError`` on read failure.
    """
    doc = _load(src)
    msp = doc.modelspace()
    counts: dict[str, int] = {}
    per_layer: dict[str, int] = {}
    for e in msp:
        counts[e.dxftype()] = counts.get(e.dxftype(), 0) + 1
        per_layer[e.dxf.layer] = per_layer.get(e.dxf.layer, 0) + 1

    extents: dict[str, Any] | None = None
    try:
        box = bbox.extents(msp)
    except (DXFError, ValueError, TypeError):
        # bbox can choke on exotic/degenerate entities; a missing bbox must not
        # fail the whole measurement — degrade to null extents.
        box = None
    if box is not None and box.has_data:
        extents = {
            "min": [box.extmin.x, box.extmin.y, box.extmin.z],
            "max": [box.extmax.x, box.extmax.y, box.extmax.z],
            "size": [box.size.x, box.size.y, box.size.z],
        }

    return {
        "entity_counts": counts,
        "entities_per_layer": per_layer,
        "extents": extents,
        "layers": sorted(layer.dxf.name for layer in doc.layers),
    }


def set_text(src: Path, old: str, new: str, dst: Path | None = None) -> dict[str, Any]:
    """Replace text content in TEXT/MTEXT entities whose value equals ``old``
    with ``new``, writing the result to ``dst`` (default ``<stem>.edited.dxf``
    next to the source — never overwrites ``src``).

    Returns ``{"output": <path>, "replaced": <count>}``. Raises ``CadError``
    if no entity matched (so the agent doesn't think a no-op succeeded).
    """
    doc = _load(src)
    msp = doc.modelspace()
    replaced = 0
    for e in msp:
        t = e.dxftype()
        if t == "TEXT" and e.dxf.text == old:
            e.dxf.text = new
            replaced += 1
        elif t == "MTEXT" and e.plain_text() == old:
            e.text = new
            replaced += 1
    if replaced == 0:
        raise CadError(f"no TEXT/MTEXT entity matched {old!r} — nothing changed")
    out = dst if dst is not None else src.with_suffix(".edited.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out))
    return {"output": str(out), "replaced": replaced}


def rename_layer(src: Path, old: str, new: str, dst: Path | None = None) -> dict[str, Any]:
    """Rename layer ``old`` to ``new`` (entities on it follow), writing to
    ``dst`` (default ``<stem>.edited.dxf``). Raises ``CadError`` if ``old``
    doesn't exist or ``new`` already does."""
    doc = _load(src)
    if old not in doc.layers:
        raise CadError(f"layer {old!r} not found")
    if new in doc.layers:
        raise CadError(f"layer {new!r} already exists")
    # rename() lives on the Layer object (it also updates entity references),
    # not on the LayerTable.
    doc.layers.get(old).rename(new)
    out = dst if dst is not None else src.with_suffix(".edited.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out))
    return {"output": str(out), "renamed": f"{old} -> {new}"}


# --- Entity creation (authoring a new drawing from scratch) ----------------
#
# Unlike set_text/rename_layer above (which edit an EXISTING drawing and
# always write a separate .edited.dxf so the original is never touched), the
# ops below author a drawing incrementally across many calls: create_document
# initializes the file, then each add_* call loads it, appends one entity,
# and saves back to the SAME path. There's no separate "original" to protect
# once create_document has made one. This is the primitive set the
# pdf-to-cad-reconstruction skill uses to turn a visually-read PDF page into
# real DXF entities.
#
# Every add_* call still goes through _load(), so the same byte/entity-count
# guards that bound a read also bound a runaway authoring sequence (e.g. an
# agent loop gone wrong) — see the module docstring above and
# docs/incidents/2026-08-04-pdf-to-dxf-oom-and-viewer-freeze.md, whose
# Prevention item 3 asked for exactly this: a sanctioned, bounded path that
# prefers real entities over per-glyph stroke tracing.


def _require_layer(doc: Any, layer: str) -> None:
    """Guard against a dangling layer reference. DXF entities can technically
    point at a layer name absent from the LAYER table (AutoCAD auto-creates
    it on open) — ezdxf allows this silently, but it produces a subtly
    malformed file rather than a clear error at authoring time. Reject it
    here instead, with the fix spelled out."""
    if layer not in doc.layers:
        raise CadError(f"layer {layer!r} does not exist — call add_layer({layer!r}) first")


def create_document(dst: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Create a new, empty DXF at ``dst`` — the starting point for authoring
    a drawing entity-by-entity with the ``add_*`` ops below (e.g.
    reconstructing a floor plan from a PDF the agent has visually read).

    Returns ``{"output": <path>}``. Raises ``CadError`` if ``dst`` already
    exists and ``overwrite`` is false (never silently clobbers prior work).
    """
    if dst.exists() and not overwrite:
        raise CadError(f"{dst} already exists — pass overwrite=True to replace it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    ezdxf.new().saveas(str(dst))
    return {"output": str(dst)}


def add_layer(path: Path, name: str, *, color: int | None = None) -> dict[str, Any]:
    """Add a named layer (e.g. ``"WALLS"``, ``"CAMERAS"``) to the DXF at
    ``path`` so later ``add_*`` calls can target it.

    ``color`` is an optional AutoCAD Color Index (1-255); omitted = default.

    Returns ``{"output": <path>, "layer": name}``. Raises ``CadError`` if the
    layer already exists.
    """
    doc = _load(path)
    if name in doc.layers:
        raise CadError(f"layer {name!r} already exists")
    doc.layers.add(name, **({} if color is None else {"color": color}))
    doc.saveas(str(path))
    return {"output": str(path), "layer": name}


def add_line(
    path: Path, start: tuple[float, float], end: tuple[float, float], *, layer: str = "0"
) -> dict[str, Any]:
    """Add a straight LINE (e.g. a wall segment) to the DXF at ``path``.

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` if ``layer`` hasn't been created (see ``add_layer``).
    """
    doc = _load(path)
    _require_layer(doc, layer)
    e = doc.modelspace().add_line(start, end, dxfattribs={"layer": layer})
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}


def add_polyline(
    path: Path, points: list[tuple[float, float]], *, closed: bool = False, layer: str = "0"
) -> dict[str, Any]:
    """Add an LWPOLYLINE (e.g. a room outline, a multi-segment wall run) to
    the DXF at ``path``. ``closed`` joins the last vertex back to the first —
    a room boundary is usually closed, a wall run usually isn't.

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` on fewer than 2 points or an unset ``layer``.
    """
    if len(points) < 2:
        raise CadError(f"a polyline needs at least 2 points, got {len(points)}")
    doc = _load(path)
    _require_layer(doc, layer)
    e = doc.modelspace().add_lwpolyline(points, close=closed, dxfattribs={"layer": layer})
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}


def add_circle(
    path: Path, center: tuple[float, float], radius: float, *, layer: str = "0"
) -> dict[str, Any]:
    """Add a CIRCLE (e.g. a camera coverage marker) to the DXF at ``path``.

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` on a non-positive ``radius`` or an unset ``layer``.
    """
    if radius <= 0:
        raise CadError(f"radius must be positive, got {radius}")
    doc = _load(path)
    _require_layer(doc, layer)
    e = doc.modelspace().add_circle(center, radius, dxfattribs={"layer": layer})
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}


def add_arc(
    path: Path,
    center: tuple[float, float],
    radius: float,
    start_angle: float,
    end_angle: float,
    *,
    layer: str = "0",
) -> dict[str, Any]:
    """Add an ARC (e.g. a door swing, a camera field-of-view wedge) to the
    DXF at ``path``. Angles are degrees, counter-clockwise from the X axis.

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` on a non-positive ``radius`` or an unset ``layer``.
    """
    if radius <= 0:
        raise CadError(f"radius must be positive, got {radius}")
    doc = _load(path)
    _require_layer(doc, layer)
    e = doc.modelspace().add_arc(
        center, radius, start_angle, end_angle, dxfattribs={"layer": layer}
    )
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}


def add_text(
    path: Path, text: str, position: tuple[float, float], *, height: float = 2.5, layer: str = "0"
) -> dict[str, Any]:
    """Add a TEXT label (e.g. a room name, a camera tag, a dimension callout)
    to the DXF at ``path`` — the real-text path the pdf-to-cad-reconstruction
    skill prefers over stroke-tracing glyphs into raw geometry (see
    docs/incidents/2026-08-04-pdf-to-dxf-oom-and-viewer-freeze.md).

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` on a non-positive ``height`` or an unset ``layer``.
    """
    if height <= 0:
        raise CadError(f"height must be positive, got {height}")
    doc = _load(path)
    _require_layer(doc, layer)
    e = doc.modelspace().add_text(
        text, height=height, dxfattribs={"insert": position, "layer": layer}
    )
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}


def add_block_insert(
    path: Path,
    block_name: str,
    position: tuple[float, float],
    *,
    layer: str = "0",
    scale: float = 1.0,
    rotation: float = 0.0,
) -> dict[str, Any]:
    """Place a reference (INSERT) to an existing block — a symbol placement,
    e.g. a camera icon defined in a template DXF the user supplied.

    Block *definition* isn't supported by this tool yet: ``block_name`` must
    already be defined in the document at ``path`` (e.g. from a template DXF
    that already carries a symbol library). If no matching block exists, use
    ``add_circle``/``add_text`` for a plain symbol instead.

    Returns ``{"output": <path>, "handle": <entity handle>}``. Raises
    ``CadError`` if ``block_name`` isn't defined, or ``layer`` isn't set.
    """
    doc = _load(path)
    if block_name not in doc.blocks:
        raise CadError(
            f"block {block_name!r} is not defined in this drawing — this tool "
            "doesn't create block definitions yet; use add_circle/add_text for "
            "a plain symbol instead"
        )
    _require_layer(doc, layer)
    e = doc.modelspace().add_blockref(
        block_name,
        position,
        dxfattribs={"layer": layer, "xscale": scale, "yscale": scale, "rotation": rotation},
    )
    doc.saveas(str(path))
    return {"output": str(path), "handle": e.dxf.handle}
