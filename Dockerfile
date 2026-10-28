FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LILYPAD_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user; /data holds the SQLite database.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin lilypad \
    && mkdir -p /data && chown lilypad:lilypad /data
USER lilypad
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# One worker on purpose: reminders and the live /dash stream run in-process.
CMD ["uvicorn", "app.main:_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--proxy-headers", "--no-server-header"]
