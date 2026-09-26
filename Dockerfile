FROM ghcr.io/astral-sh/uv:0.9 AS uv
FROM denoland/deno:bin AS deno

FROM debian:trixie-slim AS codex
# The daily channel agent (agent/run.sh) is Codex CLI on a ChatGPT login.
ARG CODEX_VERSION=0.156.1
ARG CODEX_SHA256=aff46539a83aff86e3c62c592bce2c50d95391f9df289afaf03a50c01d14533d
ADD https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz /codex.tgz
RUN echo "${CODEX_SHA256}  /codex.tgz" | sha256sum -c - \
    && tar xzf /codex.tgz -C / \
    && mv /codex-x86_64-unknown-linux-musl /codex

FROM python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=codex /codex /usr/local/bin/codex

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
