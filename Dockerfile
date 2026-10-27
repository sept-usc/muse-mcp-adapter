# Muse MCP Adapter container image (linux/amd64 and linux/arm64).
#
# - The base image is pinned by digest, and every Python dependency by version and hash
#   (requirements.lock), so a build installs exactly what was reviewed or fails.
# - The image runs as an unprivileged user and works with a read-only root filesystem: the only
#   writable places are /data (the volume that holds the adapter's state) and /tmp.
# - Nothing is downloaded when it starts.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="muse-mcp-adapter" \
      org.opencontainers.image.description="Gateway between Muse custom connectors and MCP servers" \
      org.opencontainers.image.source="https://github.com/sept-usc/muse-mcp-adapter" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH HOME=/tmp
COPY --from=build /opt/venv /opt/venv
COPY src/muse_mcp_adapter /app/src/muse_mcp_adapter
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp --shell /usr/sbin/nologin adapter \
 && mkdir -p /data /config \
 && chown 10001:10001 /data && chmod 700 /data \
 && printf '#!/bin/sh\nexec python -m muse_mcp_adapter "$@"\n' > /usr/local/bin/muse-mcp-adapter \
 && chmod 755 /usr/local/bin/muse-mcp-adapter
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["muse-mcp-adapter"]
CMD ["--database", "/data/adapter.db", "--config", "/config/serve.json", "serve"]
