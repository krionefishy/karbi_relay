FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: the layer survives every change to the source below it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY relay ./relay
RUN uv sync --frozen --no-dev

# The relay owns bot tokens; it has no business running as root.
RUN useradd --system --uid 10001 --home /app relay \
    && mkdir -p /data \
    && chown -R relay:relay /app /data
USER relay

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
