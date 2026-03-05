# syntax=docker/dockerfile:1.7

# Pin a concrete baseimage-gui release tag to avoid drift.
# Bump by checking: https://github.com/jlesage/docker-baseimage-gui/releases
FROM docker.1ms.run/jlesage/baseimage-gui:ubuntu-24.04-v4

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
    PATH=/opt/venv/bin:$PATH

# Set application name shown by baseimage-gui web UI.
RUN set-cont-env APP_NAME "StewardFlow GUI Sandbox"

# Python 3.12 is installed explicitly (not generic python3).
# Chromium is installed via Playwright and launched by /startapp.sh.
# Basic fonts avoid blank glyph rendering when Playwright screenshots pages.
COPY api/requirements.txt /tmp/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3.12 \
       python3.12-venv \
       python3-pip \
       ripgrep \
       ca-certificates \
       curl \
       git \
       fonts-dejavu-core \
       fonts-liberation \
       fonts-noto-cjk \
       fonts-noto-color-emoji \
    && python3.12 -m venv "$VENV_PATH" \
    && "$VENV_PATH/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "$VENV_PATH/bin/pip" install --no-cache-dir -r /tmp/requirements.txt \
    && "$VENV_PATH/bin/python" -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip /tmp/requirements.txt

# Create a GUI app launcher directly in-image so this Dockerfile can be copied
# and built independently without requiring external rootfs files.
# START_URL can be overridden at runtime, e.g. -e START_URL=https://google.com.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'START_URL="${START_URL:-https://www.baidu.com/}"' \
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
