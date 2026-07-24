# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder stage compiles mysqlclient (which needs
# MariaDB's dev headers and a C toolchain) into wheels; the final stage only
# installs the resulting wheels plus the MariaDB *runtime* shared library, so
# no compiler or package-manager metadata ships in the image that actually
# runs (ADR 0009).
FROM python:3.12.13-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.12.13-slim-bookworm

# libmariadb3 is the runtime counterpart of the builder's
# default-libmysqlclient-dev — mysqlclient links against it at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

COPY --from=builder /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

WORKDIR /app
COPY --chown=appuser:appuser . .

# collectstatic only needs the settings module to import cleanly — it never
# touches the database, so build-only placeholders satisfy the fail-closed
# SECRET_KEY/DB_PASSWORD checks in config/settings.py without shipping real
# secrets in the image. Running this at build time (rather than in the
# entrypoint) keeps the image immutable and means /app never needs to be
# writable at runtime.
RUN SECRET_KEY=collectstatic-build-only DB_PASSWORD=collectstatic-build-only \
        python manage.py collectstatic --noinput \
    && chmod +x docker/entrypoint.sh \
    && chown -R appuser:appuser /app/staticfiles

USER appuser
EXPOSE 8000
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
