FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PANTRY_DATA_DIR=/data \
    PANTRY_HOST=0.0.0.0 \
    PANTRY_PORT=8790 \
    PUID=1000 \
    PGID=1000

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pantry ./pantry
COPY install/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

VOLUME /data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PANTRY_PORT','8790'), timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "pantry"]
