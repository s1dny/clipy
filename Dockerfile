# syntax=docker/dockerfile:1.7
FROM denoland/deno:bin AS deno

FROM debian:trixie-slim AS tools
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp's standalone build; the upgrade workflow bumps both lines.
ARG YT_DLP_VERSION=2026.10.26
ARG YT_DLP_SHA256=767ba9dce7052aa196f8b12652f433d76de128ba168966fa84b6394d5e6c6795
# The agent: Claude Code's native build.
ARG CLAUDE_VERSION=2.1.308
ARG CLAUDE_SHA256=356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58
RUN mkdir /out \
    && curl -fsSLo /out/yt-dlp "https://github.com/yt-dlp/yt-dlp/releases/download/${YT_DLP_VERSION}/yt-dlp_linux" \
    && curl -fsSLo /out/claude "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" \
    && printf '%s  %s\n' "$YT_DLP_SHA256" /out/yt-dlp "$CLAUDE_SHA256" /out/claude | sha256sum -c - \
    && chmod 755 /out/yt-dlp /out/claude

FROM rust:1.99-slim-trixie AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY assets assets
COPY src src
COPY tests tests
# The target directory is a persistent cache, so only Clipy itself recompiles.
# The binary and the E2E test executables keep their compiled-in paths.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo test --release --locked --no-run 2>&1 | tee /tmp/tests.log \
    && cargo build --release --locked \
    && mkdir -p /out/tests \
    && cp target/release/clipy /out/clipy \
    && sed -n 's|.*Executable tests/.*(\(target/release/deps/[^)]*\)).*|\1|p' /tmp/tests.log \
       | xargs -I{} cp {} /out/tests/

# Everything Clipy and its agent run with: ffmpeg, yt-dlp with a JavaScript
# runtime for YouTube's challenges, Claude Code, and curl and jq for the
# agent's own research.
FROM debian:trixie-slim AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=tools /out/ /usr/local/bin/

FROM base AS ci
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY thumbnails/*.jpg thumbnails/
COPY agent agent
COPY tests/fixtures tests/fixtures
COPY --from=build /out/clipy target/release/clipy
COPY --from=build /out/tests/ /tests/
ENV CLIPY_TEST_CHROME=/usr/bin/chromium HOME=/tmp
CMD ["sh", "-c", "for test in /tests/*; do \"$test\" --test-threads=4 || exit 1; done"]

FROM base AS runtime
WORKDIR /app
COPY thumbnails/*.jpg thumbnails/
COPY agent agent
COPY site site
COPY --from=build /out/clipy /usr/local/bin/clipy
ENV CLIPY_DATA_DIR=/data HOME=/tmp DISABLE_AUTOUPDATER=1
ARG CLIPY_REVISION
ENV CLIPY_REVISION=$CLIPY_REVISION
LABEL org.opencontainers.image.revision=$CLIPY_REVISION
USER 10001:10001
EXPOSE 8000
CMD ["clipy", "serve"]
