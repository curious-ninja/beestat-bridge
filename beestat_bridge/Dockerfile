# Built locally by the Supervisor until prebuilt images are published.
ARG BUILD_FROM=python:3.12-slim
FROM $BUILD_FROM

WORKDIR /app
# TODO(bridge): once this lives in its own public repo, install from it so the
# Supervisor local build has the source:
#   RUN pip install --no-cache-dir "beestat-bridge @ git+https://github.com/curious-ninja/beestat-bridge"
# For now the addon build expects the source copied in by CI.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/

ENV PYTHONPATH=/app/src
CMD ["python", "-m", "beestat_bridge"]
