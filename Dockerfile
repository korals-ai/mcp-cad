# Workspace-tool sidecar (cad): a per-chat co-located helper that reads + does
# minor edits on CAD drawings, exposed as MCP tools over Streamable HTTP. The
# workspace agent calls it as mcp__workspace-tool-cad__{cad_read,cad_set_text,
# cad_rename_layer} instead of carrying CAD libraries in the workspace image.
#
# This phase: DXF only (ezdxf, MIT, pip-only — no apt toolchain). DWG read is
# a deferred follow-up that adds a from-source LibreDWG build (dwg2dxf) — see
# docs/plan/20260619-200506-toolspace-sidecar.md §6f.

FROM python:3.12-alpine AS builder

WORKDIR /app

RUN apk add --no-cache --virtual .build-deps \
      gcc musl-dev

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.12-alpine

# ezdxf is pure-Python (its optional C-extensions build only if a compiler is
# present, which we don't add in the runtime; the pure-Python fallback is fully
# functional). No apt toolchain needed — lightest tool sidecar.

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY src ./src

ENV PYTHONPATH=/app

# Mirror the workspace pod's unprivileged identity (uid/gid 65532) so files
# this sidecar writes onto the shared tenant PVC carry the ownership the main
# container expects (fsGroup 65532).
RUN addgroup -g 65532 tool \
 && adduser -D -u 65532 -G tool -h /home/tool -s /bin/bash tool \
 && mkdir -p /home/tool \
 && chown -R tool:tool /home/tool
ENV HOME=/home/tool

EXPOSE 8092

USER tool

ENTRYPOINT ["python", "-m", "src.server"]
