# syntax=docker/dockerfile:1
# Reproducible non-root webhook-inbox container.
# Build:   docker build -t webhook-inbox .
# Run:     docker run --rm -p 8000:8000 -e WEBHOOK_SECRET=devpw webhook-inbox
# Health:  HEALTHCHECK queries /health; container user is non-root (uid 1001).

FROM python:3.13-slim AS src
WORKDIR /app
COPY requirements.txt .
COPY requirements.in .
COPY pyproject.toml .
COPY src ./src

FROM src AS builder
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN python -m pip install --upgrade pip \
    && pip install --require-hashes --no-deps --no-cache-dir -r requirements.txt \
    && pip install --no-deps --no-cache-dir .

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/opt/venv/bin:$PATH \
    DATABASE_PATH=/data/inbox.db
WORKDIR /data
COPY --from=builder /opt/venv /opt/venv
COPY src /app/src
COPY compose.py /app/compose.py
RUN groupadd --system app && useradd --system --gid app --home /data --shell /sbin/nologin -u 1001 app \
    && mkdir -p /data && chown -R app:app /data /app
USER app
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"
CMD ["uvicorn", "compose:app", "--host", "0.0.0.0", "--port", "8000"]
