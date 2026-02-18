ARG PYTHON_VERSION=3.12
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS base

FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev pkg-config build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app

RUN uv venv $VIRTUAL_ENV
COPY uv.lock pyproject.toml ./
COPY vendor/ vendor/
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS final
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app
COPY --from=builder /venv /venv
COPY . .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
