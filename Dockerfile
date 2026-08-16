# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Multi-architecture index digests, not mutable tags. Updating a runtime is an
# explicit reviewed change that regenerates the SBOM/provenance in CI.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY platform_core ./platform_core
COPY workloads ./workloads

# The lock contains artifact hashes for every wheel/sdist. `--frozen` refuses
# to resolve or silently rewrite it during an image build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

ARG RELEASE=unknown
LABEL org.opencontainers.image.title="local-platform" \
      org.opencontainers.image.revision="${RELEASE}" \
      org.opencontainers.image.source="local-platform-codex"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    RELEASE="${RELEASE}"

RUN groupadd --gid 10001 platform \
    && useradd --uid 10001 --gid platform --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin platform

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 apps ./apps
COPY --chown=10001:10001 platform_core ./platform_core
COPY --chown=10001:10001 workloads ./workloads
# The same immutable image runs the migration Job, API, worker and relay. Only
# the migration Job receives the owner DSN.
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 deploy/migrations ./deploy/migrations

USER 10001:10001
EXPOSE 8100
STOPSIGNAL SIGTERM

CMD ["python", "-m", "apps.api.main"]
