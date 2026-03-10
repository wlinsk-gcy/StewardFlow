# syntax=docker/dockerfile:1.7

# Pin a concrete baseimage-gui release tag to avoid drift.
# Bump by checking: https://github.com/jlesage/docker-baseimage-gui/releases
FROM jlesage/baseimage-gui:ubuntu-24.04-v4

# Upstream docs references (baseimage-gui):
# - Init/service model and custom services under /etc/services.d:
#   https://github.com/jlesage/docker-baseimage-gui#container-startup
#   https://github.com/jlesage/docker-baseimage-gui#setting-up-a-service
# - Containerized GUI app entrypoint via /startapp.sh:
#   https://github.com/jlesage/docker-baseimage-gui#setting-the-containerized-application
# - noVNC/VNC default ports:
#   https://github.com/jlesage/docker-baseimage-gui#accessing-the-gui
# - Persistent config/data and user permission mapping:
#   https://github.com/jlesage/docker-baseimage-gui#data-volumes
#   https://github.com/jlesage/docker-baseimage-gui#usergroup-ids

ENV DEBIAN_FRONTEND=noninteractive \
    VENV_PATH=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    PYTHONUNBUFFERED=1 \
    CHROME_REMOTE_DEBUGGING_PORT=9222 \
    SANDBOX_ARTIFACT_ROOT=/config/tool-artifacts \
    SANDBOX_UPLOAD_ROOT=/config/uploads \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=8 \
    PATH=/opt/venv/bin:$PATH

# Set application name shown by baseimage-gui web UI.
RUN set-cont-env APP_NAME "StewardFlow GUI Sandbox"

# Python 3.12 is installed explicitly (not generic python3).
# Chromium is installed via Playwright and launched by /startapp.sh.
# Basic fonts avoid blank glyph rendering when Playwright screenshots pages.
COPY api/requirements.txt /tmp/requirements.txt
ARG UBUNTU_APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu/
ARG NODE_VERSION=24.14.0
ARG NODE_DIST_BASE_URL=https://nodejs.org/dist
ARG NODE_DOWNLOAD_CONNECT_TIMEOUT=30
ARG NODE_DOWNLOAD_RETRY=5
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
ARG PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=300000

# Force container-side Ubuntu packages to use a faster mirror.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
      printf 'Types: deb\nURIs: %s\nSuites: noble noble-updates noble-backports\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\nTypes: deb\nURIs: %s\nSuites: noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        "$UBUNTU_APT_MIRROR" "$UBUNTU_APT_MIRROR" \
        > /etc/apt/sources.list.d/ubuntu.sources; \
      rm -f /etc/apt/sources.list; \
    elif [ -f /etc/apt/sources.list ]; then \
      printf 'deb %s noble main restricted universe multiverse\ndeb %s noble-updates main restricted universe multiverse\ndeb %s noble-backports main restricted universe multiverse\ndeb %s noble-security main restricted universe multiverse\n' \
        "$UBUNTU_APT_MIRROR" "$UBUNTU_APT_MIRROR" "$UBUNTU_APT_MIRROR" "$UBUNTU_APT_MIRROR" \
        > /etc/apt/sources.list; \
    fi

# Base Ubuntu dependencies and fonts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       gpg \
       python3.12 \
       python3.12-venv \
       python3-pip \
       ripgrep \
       git \
       fonts-dejavu-core \
       fonts-liberation \
       fonts-noto-cjk \
       fonts-noto-color-emoji \
    && python3.12 -m venv "$VENV_PATH" \
    && rm -rf /var/lib/apt/lists/*

# Install a fixed Node.js LTS tarball to avoid NodeSource APT bottlenecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       xz-utils \
    && ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) NODE_ARCH="x64" ;; \
         arm64) NODE_ARCH="arm64" ;; \
         *) echo "Unsupported Node.js architecture: $ARCH" >&2; exit 1 ;; \
       esac \
    && NODE_DIST="node-v${NODE_VERSION}-linux-${NODE_ARCH}" \
    && echo "Downloading ${NODE_DIST}.tar.xz from ${NODE_DIST_BASE_URL}/v${NODE_VERSION}/" \
    && curl -fL --progress-bar \
       --connect-timeout "${NODE_DOWNLOAD_CONNECT_TIMEOUT}" \
       --retry "${NODE_DOWNLOAD_RETRY}" \
       --retry-delay 2 \
       --retry-all-errors \
       -o "${NODE_DIST}.tar.xz" \
       "${NODE_DIST_BASE_URL}/v${NODE_VERSION}/${NODE_DIST}.tar.xz" \
    && echo "Downloading SHASUMS256.txt from ${NODE_DIST_BASE_URL}/v${NODE_VERSION}/" \
    && curl -fL --progress-bar \
       --connect-timeout "${NODE_DOWNLOAD_CONNECT_TIMEOUT}" \
       --retry "${NODE_DOWNLOAD_RETRY}" \
       --retry-delay 2 \
       --retry-all-errors \
       -o SHASUMS256.txt \
       "${NODE_DIST_BASE_URL}/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " ${NODE_DIST}.tar.xz\$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf "${NODE_DIST}.tar.xz" -C /usr/local --strip-components=1 \
    && node --version \
    && npm --version \
    && rm -f "${NODE_DIST}.tar.xz" SHASUMS256.txt \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies.
RUN "$VENV_PATH/bin/pip" install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt \
    && rm -rf /root/.cache/pip /tmp/requirements.txt

# Playwright supports custom download hosts and connection timeout via env vars.
# Default to the verified npmmirror binary CDN for the pinned Playwright build;
# callers can still override PLAYWRIGHT_DOWNLOAD_HOST at build time when needed.
RUN if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then \
      export PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST"; \
      echo "Using custom Playwright download host: $PLAYWRIGHT_DOWNLOAD_HOST"; \
    else \
      echo "Using default Playwright download host"; \
    fi \
    && PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="$PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT" \
       "$VENV_PATH/bin/python" -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Create a GUI app launcher directly in-image so this Dockerfile can be copied
# and built independently without requiring external rootfs files.
# START_URL can be overridden at runtime, e.g. -e START_URL=https://google.com.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'START_URL="${START_URL:-chrome://new-tab-page/}"' \
    'WIDTH="${DISPLAY_WIDTH:-1920}"' \
    'HEIGHT="${DISPLAY_HEIGHT:-1080}"' \
    'CDP_PORT="${CHROME_REMOTE_DEBUGGING_PORT:-9222}"' \
    'CHROME_BIN="$(find /opt/playwright-browsers -type f -name chrome | head -n1 || true)"' \
    'if [ -z "$CHROME_BIN" ]; then' \
    '  echo "Chromium binary not found under /opt/playwright-browsers" >&2' \
    '  exit 1' \
    'fi' \
    'exec "$CHROME_BIN" --no-sandbox --disable-dev-shm-usage --disable-gpu --no-first-run --no-default-browser-check --remote-debugging-address=127.0.0.1 --remote-debugging-port="${CDP_PORT}" --user-data-dir=/tmp/chrome-data --window-position=0,0 --window-size="${WIDTH},${HEIGHT}" --start-fullscreen "$START_URL"' \
    > /startapp.sh \
    && chmod 0755 /startapp.sh \
    && test -x /startapp.sh \
    && mkdir -p /opt/sandbox /workspace /config/tool-artifacts /config/uploads

# API service runs under baseimage-gui supervisor model from /etc/services.d.
COPY api /opt/sandbox/api
RUN mkdir -p /etc/services.d/sandbox-api /etc/services.d/default \
    && printf '%s\n' \
       '#!/bin/sh' \
       'set -eu' \
       'cd /opt/sandbox' \
       'exec /opt/venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8899' \
       > /etc/services.d/sandbox-api/run \
    && chmod 0755 /etc/services.d/sandbox-api/run \
    && : > /etc/services.d/default/sandbox-api.dep

# baseimage-gui defaults:
# - 5800: noVNC web UI
# - 5900: raw VNC
# Sandbox API:
# - 8899
EXPOSE 5800 5900 8899
