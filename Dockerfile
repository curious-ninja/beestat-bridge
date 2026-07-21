FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/

ENV PYTHONPATH=/app/src \
    BRIDGE_DATA=/data
VOLUME /data
EXPOSE 8127

CMD ["python", "-m", "beestat_bridge"]
