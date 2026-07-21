# Home Assistant local build. Context is THIS folder (beestat_bridge/), which
# does not contain the Python source — that lives at the repo root — so the
# source is cloned at build time. Base image is pinned (not HA's BUILD_FROM)
# so the Debian/apt toolchain is predictable across arches.
FROM python:3.12-slim

# Override to build a different fork/branch of the bridge.
ARG BRIDGE_REPO=https://github.com/curious-ninja/beestat-bridge
ARG BRIDGE_REF=main

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone --depth 1 --branch "${BRIDGE_REF}" "${BRIDGE_REPO}" /tmp/bridge \
    && cp -r /tmp/bridge/src /app/src \
    && pip install --no-cache-dir -r /tmp/bridge/requirements.txt \
    && rm -rf /tmp/bridge

ENV PYTHONPATH=/app/src \
    BRIDGE_DATA=/data
VOLUME /data
EXPOSE 8127
CMD ["python", "-m", "beestat_bridge"]
