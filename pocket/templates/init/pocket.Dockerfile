ARG PYTHON_VERSION=3.12
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app

RUN uv venv $VIRTUAL_ENV
COPY uv.lock pyproject.toml ./
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app
COPY --from=builder /venv /venv
COPY . .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
