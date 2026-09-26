FROM ghcr.io/astral-sh/uv:0.9 AS uv
FROM denoland/deno:bin AS deno

FROM debian:trixie-slim AS codex
# The daily channel agent (agent/run.sh) is Codex CLI on a ChatGPT login.
# Codex runs tool calls through codex-code-mode-host and sandboxes commands with
# bwrap; both ship as separate release assets and must sit beside codex.
ARG CODEX_VERSION=0.156.1
ARG CODEX_RELEASE=https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}
ADD ${CODEX_RELEASE}/codex-x86_64-unknown-linux-musl.tar.gz /codex.tgz
ADD ${CODEX_RELEASE}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz /host.tgz
ADD ${CODEX_RELEASE}/bwrap-x86_64-unknown-linux-musl.tar.gz /bwrap.tgz
RUN printf '%s  %s\n' \
        aff46539a83aff86e3c62c592bce2c50d95391f9df289afaf03a50c01d14533d /codex.tgz \
        a929daa9f6a0bddc00c0c9e6402df117b125acd96f9d554f6c99c32c7e66c608 /host.tgz \
        74bb868f29e845da36ab287b88459a5fbb1fa4e16ff59837660312eaf0b33f3a /bwrap.tgz \
        | sha256sum -c - \
    && mkdir /out \
    && for f in /codex.tgz /host.tgz /bwrap.tgz; do tar xzf "$f" -C /out; done \
    && mv /out/codex-x86_64-unknown-linux-musl /out/codex \
    && mv /out/codex-code-mode-host-x86_64-unknown-linux-musl /out/codex-code-mode-host \
    && mv /out/bwrap-x86_64-unknown-linux-musl /out/bwrap

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
COPY clipper ./clipper
COPY thumbnails/*.jpg ./thumbnails/
COPY agent ./agent
COPY main.py README.md ./
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CLIPPER_DATA_DIR=/data \
    HOME=/tmp
USER 10001:10001
EXPOSE 8000
CMD ["clipper-mcp"]
