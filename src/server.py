"""Workspace-tool sidecar (cad) — MCP server over Streamable HTTP.

Exposes CAD read/extract + minor-edit + authoring (DXF, via ezdxf) as MCP
tools the workspace agent calls over
``http://localhost:<WORKSPACE_TOOL_PORT>/mcp``. Same substrate as office/ocr:
co-located in the workspace pod, shares the tenant PVC, so files stay on the
shared mount and the RPC carries only paths + verdicts.

DXF only this phase; DWG read is a deferred follow-up (from-source LibreDWG —
see docs/plan §6f). Standalone default port 8092 (office 8090, ocr 8091); the
operator injects WORKSPACE_TOOL_PORT for the real per-pod port.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from src import cad_ops
from src.cad_ops import CadError

log = logging.getLogger("workspace-tool-cad")

HOST = os.environ.get("WORKSPACE_TOOL_HOST", "0.0.0.0")  # noqa: S104 - pod-local, reached via localhost
PORT = int(os.environ.get("WORKSPACE_TOOL_PORT", "8092"))

mcp = FastMCP("cad", host=HOST, port=PORT)


def _log(op: str, src: str, started: float, *, error: Exception | None = None) -> None:
    """One structured line per call (keys match office/ocr) so Loki can chart
    error rate without per-pod scraping."""
    dur_ms = int((time.monotonic() - started) * 1000)
    name = Path(src).name
    if error is None:
        log.info("tool=cad op=%s outcome=ok dur_ms=%d src=%s", op, dur_ms, name)
    else:
        log.warning("tool=cad op=%s outcome=error dur_ms=%d src=%s err=%s", op, dur_ms, name, error)


@mcp.tool()
def cad_read(src: str) -> dict:
    """Read the content of a DXF CAD drawing (floor plan, camera diagram).

    Returns the drawing's DXF version, layer names, a per-type entity count,
    and all text labels (TEXT/MTEXT) — the content to reason about on a tender
    plan (camera labels, room names, notes).

    Args:
        src: Absolute path to the DXF on the shared workspace volume
            (e.g. ``/home/agent/plan.dxf``). DWG is not supported yet.

    Returns:
        ``{dxf_version, layers, entity_counts, text}``.

    Raises:
        Read failures (DWG, missing/corrupt source, not a DXF) surface as an
        MCP tool error.
    """
    started = time.monotonic()
    try:
        out = cad_ops.read_summary(Path(src))
    except CadError as exc:
        _log("cad_read", src, started, error=exc)
        raise
    _log("cad_read", src, started)
    return out


@mcp.tool()
def cad_list_entities(src: str, kind: str = "") -> dict:
    """List the entities in a DXF drawing, optionally filtered to one type.

    Use this to inventory what's on a plan — how many cameras (INSERT block
    refs), text labels, lines, etc. — without installing any CAD library.

    Args:
        src: Absolute path to the DXF on the shared workspace volume.
        kind: Optional DXF type filter, case-insensitive (e.g. ``"LINE"``,
            ``"INSERT"``, ``"MTEXT"``). Empty = all entities.

    Returns:
        ``{count, entities}`` — each entity has ``type``, ``layer``, ``handle``,
        plus ``text`` (TEXT/MTEXT) or ``block`` (INSERT) where applicable.

    Raises:
        An MCP tool error on a read failure (DWG, missing/corrupt source).
    """
    started = time.monotonic()
    try:
        out = cad_ops.list_entities(Path(src), kind or None)
    except CadError as exc:
        _log("cad_list_entities", src, started, error=exc)
        raise
    _log("cad_list_entities", src, started)
    return out


@mcp.tool()
def cad_extract_geometry(src: str) -> dict:
    """Extract coordinates + lengths of the geometric primitives (LINE,
    LWPOLYLINE, CIRCLE, ARC) in a DXF — walls, room outlines, camera cones.

    Use this for spatial reasoning (distances, coverage, run lengths) instead
    of installing ezdxf to parse the file yourself.

    Args:
        src: Absolute path to the DXF on the shared workspace volume.

    Returns:
        ``{count, total_line_length, geometry}`` — one row per primitive with
        its coordinates and (for lines/polylines) length.

    Raises:
        An MCP tool error on a read failure.
    """
    started = time.monotonic()
    try:
        out = cad_ops.extract_geometry(Path(src))
    except CadError as exc:
        _log("cad_extract_geometry", src, started, error=exc)
        raise
    _log("cad_extract_geometry", src, started)
    return out


@mcp.tool()
def cad_measure(src: str) -> dict:
    """Measure a DXF drawing: overall bounding box (extents + size), per-type
    entity counts, per-layer counts, and the layer list — the one-call
    "how big is this plan and what's in it" overview.

    Args:
        src: Absolute path to the DXF on the shared workspace volume.

    Returns:
        ``{entity_counts, entities_per_layer, extents, layers}``; ``extents`` is
        ``null`` for an empty drawing.

    Raises:
        An MCP tool error on a read failure.
    """
    started = time.monotonic()
    try:
        out = cad_ops.measure(Path(src))
    except CadError as exc:
        _log("cad_measure", src, started, error=exc)
        raise
    _log("cad_measure", src, started)
    return out


@mcp.tool()
def cad_set_text(src: str, old: str, new: str) -> dict:
    """Replace text in a DXF: every TEXT/MTEXT whose value equals ``old``
    becomes ``new``. Writes ``<stem>.edited.dxf`` next to the source (never
    overwrites it).

    Args:
        src: Absolute path to the DXF on the shared volume.
        old: Exact current text to match.
        new: Replacement text.

    Returns:
        ``{output, replaced}`` — the written path and how many entities changed.

    Raises:
        An MCP tool error if nothing matched (so a no-op isn't read as success).
    """
    started = time.monotonic()
    try:
        out = cad_ops.set_text(Path(src), old, new)
    except CadError as exc:
        _log("cad_set_text", src, started, error=exc)
        raise
    _log("cad_set_text", src, started)
    return out


@mcp.tool()
def cad_rename_layer(src: str, old: str, new: str) -> dict:
    """Rename a layer in a DXF (entities on it follow). Writes
    ``<stem>.edited.dxf`` next to the source.

    Args:
        src: Absolute path to the DXF on the shared volume.
        old: Existing layer name.
        new: New layer name (must not already exist).

    Returns:
        ``{output, renamed}``.

    Raises:
        An MCP tool error if ``old`` is missing or ``new`` already exists.
    """
    started = time.monotonic()
    try:
        out = cad_ops.rename_layer(Path(src), old, new)
    except CadError as exc:
        _log("cad_rename_layer", src, started, error=exc)
        raise
    _log("cad_rename_layer", src, started)
    return out


@mcp.tool()
def cad_create_document(dst: str, overwrite: bool = False) -> dict:
    """Create a new, empty DXF — the starting point for authoring a drawing
    entity-by-entity with the other ``cad_add_*`` tools (e.g. reconstructing
    a technical drawing from a PDF via ``pdf_extract_text``/``pdf_to_images``;
    see the ``pdf-to-cad-reconstruction`` skill).

    If this is for PDF-to-CAD reconstruction: that whole pipeline is a free,
    best-effort aid, NOT a commercial-grade vectorization product — tell the
    user plainly that the result is a starting point to verify, not a
    certified redraw (see the skill for why, incl. a confirmed real failure
    case).

    Args:
        dst: Absolute path to create on the shared workspace volume.
        overwrite: If false (default) and ``dst`` already exists, this call
            errors instead of silently clobbering it.

    Returns:
        ``{output}``.

    Raises:
        An MCP tool error if ``dst`` already exists and ``overwrite`` is false.
    """
    started = time.monotonic()
    try:
        out = cad_ops.create_document(Path(dst), overwrite=overwrite)
    except CadError as exc:
        _log("cad_create_document", dst, started, error=exc)
        raise
    _log("cad_create_document", dst, started)
    return out


@mcp.tool()
def cad_add_layer(path: str, name: str, color: int = 0) -> dict:
    """Add a named layer (e.g. ``"WALLS"``, ``"CAMERAS"``) to a DXF being
    authored, so later ``cad_add_*`` calls can target it.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        name: Layer name to create.
        color: Optional AutoCAD Color Index (1-255); 0 = default.

    Returns:
        ``{output, layer}``.

    Raises:
        An MCP tool error if the layer already exists.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_layer(Path(path), name, color=color or None)
    except CadError as exc:
        _log("cad_add_layer", path, started, error=exc)
        raise
    _log("cad_add_layer", path, started)
    return out


@mcp.tool()
def cad_add_line(path: str, start: list[float], end: list[float], layer: str = "0") -> dict:
    """Add a straight LINE (e.g. a wall segment) to a DXF being authored.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        start: ``[x, y]`` start point.
        end: ``[x, y]`` end point.
        layer: Target layer — must already exist (see ``cad_add_layer``);
            ``"0"`` (the DXF default layer) always exists.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error if ``layer`` doesn't exist.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_line(Path(path), (start[0], start[1]), (end[0], end[1]), layer=layer)
    except CadError as exc:
        _log("cad_add_line", path, started, error=exc)
        raise
    _log("cad_add_line", path, started)
    return out


@mcp.tool()
def cad_add_polyline(
    path: str, points: list[list[float]], closed: bool = False, layer: str = "0"
) -> dict:
    """Add an LWPOLYLINE (e.g. a room outline, a multi-segment wall run) to a
    DXF being authored.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        points: ``[[x, y], ...]`` vertices, at least 2.
        closed: Join the last vertex back to the first (a room boundary is
            usually closed, a wall run usually isn't).
        layer: Target layer — must already exist.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error on fewer than 2 points or an unset layer.
    """
    started = time.monotonic()
    try:
        pts = [(p[0], p[1]) for p in points]
        out = cad_ops.add_polyline(Path(path), pts, closed=closed, layer=layer)
    except CadError as exc:
        _log("cad_add_polyline", path, started, error=exc)
        raise
    _log("cad_add_polyline", path, started)
    return out


@mcp.tool()
def cad_add_circle(path: str, center: list[float], radius: float, layer: str = "0") -> dict:
    """Add a CIRCLE (e.g. a camera coverage marker) to a DXF being authored.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        center: ``[x, y]`` center point.
        radius: Must be positive.
        layer: Target layer — must already exist.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error on a non-positive radius or an unset layer.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_circle(Path(path), (center[0], center[1]), radius, layer=layer)
    except CadError as exc:
        _log("cad_add_circle", path, started, error=exc)
        raise
    _log("cad_add_circle", path, started)
    return out


@mcp.tool()
def cad_add_arc(
    path: str,
    center: list[float],
    radius: float,
    start_angle: float,
    end_angle: float,
    layer: str = "0",
) -> dict:
    """Add an ARC (e.g. a door swing, a camera field-of-view wedge) to a DXF
    being authored. Angles are degrees, counter-clockwise from the X axis.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        center: ``[x, y]`` center point.
        radius: Must be positive.
        start_angle: Arc start angle in degrees.
        end_angle: Arc end angle in degrees.
        layer: Target layer — must already exist.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error on a non-positive radius or an unset layer.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_arc(
            Path(path), (center[0], center[1]), radius, start_angle, end_angle, layer=layer
        )
    except CadError as exc:
        _log("cad_add_arc", path, started, error=exc)
        raise
    _log("cad_add_arc", path, started)
    return out


@mcp.tool()
def cad_add_text(
    path: str, text: str, position: list[float], height: float = 2.5, layer: str = "0"
) -> dict:
    """Add a TEXT label (e.g. a room name, a camera tag, a dimension
    callout) to a DXF being authored. Prefer this over stroke-tracing text
    into raw line geometry — it keeps the file small and the text real
    (searchable/editable), and avoids the OOM failure mode this tool guards
    against (see ``_load``'s size caps).

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        text: The label text.
        position: ``[x, y]`` insertion point.
        height: Text height; must be positive.
        layer: Target layer — must already exist.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error on a non-positive height or an unset layer.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_text(
            Path(path), text, (position[0], position[1]), height=height, layer=layer
        )
    except CadError as exc:
        _log("cad_add_text", path, started, error=exc)
        raise
    _log("cad_add_text", path, started)
    return out


@mcp.tool()
def cad_add_block_insert(
    path: str,
    block_name: str,
    position: list[float],
    layer: str = "0",
    scale: float = 1.0,
    rotation: float = 0.0,
) -> dict:
    """Place a reference (INSERT) to an existing block — a symbol placement,
    e.g. a camera icon defined in a template DXF the user supplied. This
    tool does NOT create block definitions; ``block_name`` must already be
    defined in the document at ``path``. If no matching block exists, use
    ``cad_add_circle``/``cad_add_text`` for a plain symbol instead.

    Args:
        path: Absolute path to the DXF on the shared workspace volume.
        block_name: Name of an already-defined block.
        position: ``[x, y]`` insertion point.
        layer: Target layer — must already exist.
        scale: Uniform X/Y scale factor.
        rotation: Rotation in degrees.

    Returns:
        ``{output, handle}``.

    Raises:
        An MCP tool error if ``block_name`` isn't defined or ``layer`` isn't set.
    """
    started = time.monotonic()
    try:
        out = cad_ops.add_block_insert(
            Path(path),
            block_name,
            (position[0], position[1]),
            layer=layer,
            scale=scale,
            rotation=rotation,
        )
    except CadError as exc:
        _log("cad_add_block_insert", path, started, error=exc)
        raise
    _log("cad_add_block_insert", path, started)
    return out


def main() -> None:
    """Run the MCP server forever over Streamable HTTP. Blocks; entrypoint."""
    logging.basicConfig(level=logging.INFO)
    log.info(
        "workspace-tool-cad MCP server on %s:%d (/mcp) — DXF read/extract+edit+author",
        HOST,
        PORT,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
