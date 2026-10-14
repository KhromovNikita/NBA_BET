FROM python:3.14.8-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra ml --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
COPY clickhouse ./clickhouse
RUN uv sync --frozen --extra ml --no-dev

USER app
ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["-m", "scripts.train_models", "--help"]
