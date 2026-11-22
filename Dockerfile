FROM python:3.12-slim

# Optional PyPI mirror for air-gapped builds.
ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime dependencies only: requirements.txt lists dev tools after the
# "# Development" header.
COPY requirements.txt .
RUN [ -n "$PIP_INDEX_URL" ] || unset PIP_INDEX_URL; \
    sed '/^# Development/,$d' requirements.txt > runtime-requirements.txt \
    && pip install --no-cache-dir -r runtime-requirements.txt \
    && rm runtime-requirements.txt

COPY app ./app

# Default REGISTRY_DATA_DIR (./data) must be writable without a volume too.
RUN mkdir -p data && chown 1001 data

USER 1001
EXPOSE 3000
# One worker (python -m app runs uvicorn with workers=1): sessions are in
# process memory and only one process may use the registry file.
CMD ["python", "-m", "app"]
