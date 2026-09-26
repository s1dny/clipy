FROM ghcr.io/astral-sh/uv:0.9 AS uv
FROM denoland/deno:bin AS deno

FROM python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY clipper ./clipper
COPY thumbnails/*.jpg ./thumbnails/
COPY main.py README.md ./
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CLIPPER_DATA_DIR=/data \
    HOME=/tmp
USER 10001:10001
EXPOSE 8000
CMD ["clipper-mcp"]
