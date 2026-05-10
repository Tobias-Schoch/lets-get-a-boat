FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "jinja2>=3.1" \
      "python-multipart>=0.0.12" \
      "httpx>=0.27" \
      "beautifulsoup4>=4.12" \
      "lxml>=5.3" \
      "sqlalchemy>=2.0" \
      "apscheduler>=3.10" \
      "pydantic>=2.9" \
      "pydantic-settings>=2.6"

COPY app ./app

RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
