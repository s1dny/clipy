FROM ghcr.io/astral-sh/uv:0.12 AS uv
FROM denoland/deno:bin AS deno

FROM debian:trixie-slim AS codex
# The agent (agent/run.sh) is Codex CLI on a ChatGPT login.
# Codex runs tool calls through codex-code-mode-host, which ships as a separate
# release asset and must sit beside codex.
ARG CODEX_VERSION=0.160.0
ARG CODEX_RELEASE=https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}
ADD ${CODEX_RELEASE}/codex-x86_64-unknown-linux-musl.tar.gz /codex.tgz
ADD ${CODEX_RELEASE}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz /host.tgz
RUN printf '%s  %s\n' \
        306865417d4ee7a927785852910a527f41e1e159add390ac5ae3accb67d44a13 /codex.tgz \
        ac6cd6288f0e39f46a33eba1cfd37711651f14f33dbc73d6a1b82eb45e038cac /host.tgz \
        | sha256sum -c - \
    && mkdir /out \
    && for f in /codex.tgz /host.tgz; do tar xzf "$f" -C /out; done \
    && mv /out/codex-x86_64-unknown-linux-musl /out/codex \
    && mv /out/codex-code-mode-host-x86_64-unknown-linux-musl /out/codex-code-mode-host

FROM python:3.14-slim-trixie AS dependencies
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

# Expensive tools and dependencies are cached independently of source edits.
FROM dependencies AS ci-dependencies
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project
RUN /app/.venv/bin/playwright install --with-deps chromium

FROM ci-dependencies AS ci
COPY clipy ./clipy
COPY tests ./tests
COPY thumbnails/*.jpg ./thumbnails/
COPY agent ./agent
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 HOME=/tmp
CMD ["pytest", "-n", "4", "--dist", "loadgroup", "-q", "--durations=10", "tests"]

FROM dependencies AS runtime-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
FROM runtime-dependencies AS runtime
COPY clipy ./clipy
COPY thumbnails/*.jpg ./thumbnails/
COPY agent ./agent
COPY README.md ./
COPY site ./site
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CLIPY_DATA_DIR=/data \
    HOME=/tmp
ARG CLIPY_REVISION
ENV CLIPY_REVISION=$CLIPY_REVISION
LABEL org.opencontainers.image.revision=$CLIPY_REVISION
USER 10001:10001
EXPOSE 8000
CMD ["clipy-mcp"]
