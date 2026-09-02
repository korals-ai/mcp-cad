# mcp-cad

Read, measure, edit and author DXF drawings. An [MCP](https://modelcontextprotocol.io) server speaking Streamable
HTTP: run it in a container, point your agent at `http://localhost:8092/mcp`.

CAD work over DXF (R12–R2018) via ezdxf: read a drawing's structure, list and extract entity geometry, measure, retitle text and rename layers, and author new documents from primitives. Pure Python — no CAD application, no license server. DWG is not supported.

## Quickstart

```bash
docker compose up          # builds the image the first time
```

Then register it with your agent. Claude Code:

```bash
claude mcp add --transport http cad http://localhost:8092/mcp
```

…or in a client config:

```json
{"mcpServers": {"cad": {"type": "http", "url": "http://localhost:8092/mcp"}}}
```

## How files reach the tools

These tools take **paths, not uploads** — the agent names a file, the server
opens it in place and writes results back. Nothing but the path and the verdict
crosses the MCP wire, so a 200 MB file costs no tokens.

That means the container has to be able to see your files. `docker compose up`
mounts the directory you ran it from at `/work`, so tell the agent about
`/work/drawing.dxf`, not `~/drawing.dxf`. Mount somewhere else with
`WORKDIR=/path/to/project docker compose up`.

Files the tools create are written as your host user, not root:

```bash
MCP_UID=$(id -u) MCP_GID=$(id -g) docker compose up   # if your uid is not 1000
```

## Tools

- `cad_read`
- `cad_list_entities`
- `cad_extract_geometry`
- `cad_measure`
- `cad_set_text`
- `cad_rename_layer`
- `cad_create_document`
- `cad_add_layer`
- `cad_add_line`
- `cad_add_polyline`
- `cad_add_circle`
- `cad_add_arc`
- `cad_add_text`
- `cad_add_block_insert`

Each tool's own description and typed signature — what the agent actually reads
to decide when to call it — is in `src/server.py`.

## Requirements

Nothing outside Python — the image is small and builds in under a minute.

## Contributing

Issues and PRs are welcome and read directly.

One thing to know before you send a PR: this repository is a **one-way mirror**
of a directory in a private monorepo, which stays canonical. Contributions are
applied there and reappear here on the next sync, so your change lands with your
authorship upstream but arrives in this repo's history inside a sync commit.
Nothing here is force-pushed away, but don't expect your PR to be merged with a
green button.

## License

Apache-2.0 — see [LICENSE](LICENSE).
