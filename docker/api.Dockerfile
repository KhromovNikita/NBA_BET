FROM python:3.12.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "nba_live_totals.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
