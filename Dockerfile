FROM ghcr.io/astral-sh/uv:0.12 AS uv
FROM denoland/deno:bin AS deno

FROM debian:trixie-slim AS codex
# The agent (agent/run.sh) is Codex CLI on a ChatGPT login.
# Codex runs tool calls through codex-code-mode-host, which ships as a separate
# release asset and must sit beside codex.
ARG CODEX_VERSION=0.158.0
ARG CODEX_RELEASE=https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}
ADD ${CODEX_RELEASE}/codex-x86_64-unknown-linux-musl.tar.gz /codex.tgz
ADD ${CODEX_RELEASE}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz /host.tgz
RUN printf '%s  %s\n' \
        af9f5aa6e6662accf9d707cef0d9ca083880a173a9c2b6c22947edb7830e5778 /codex.tgz \
        5455c64be4ba371444710895aff6d74d653985a3bfc454a8b7fe42d6e6d11e3d /host.tgz \
        | sha256sum -c - \
    && mkdir /out \
    && for f in /codex.tgz /host.tgz; do tar xzf "$f" -C /out; done \
    && mv /out/codex-x86_64-unknown-linux-musl /out/codex \
    && mv /out/codex-code-mode-host-x86_64-unknown-linux-musl /out/codex-code-mode-host

FROM python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=codex /out/ /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY clipy ./clipy
COPY thumbnails/*.jpg ./thumbnails/
COPY agent ./agent
COPY README.md ./
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CLIPY_DATA_DIR=/data \
    HOME=/tmp
USER 10001:10001
EXPOSE 8000
CMD ["clipy-mcp"]
