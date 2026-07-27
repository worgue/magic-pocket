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
COPY --chmod=644 uv.lock pyproject.toml ./
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app
COPY --from=builder /venv /venv
# --chmod: host 側で mode 600 になったファイルをそのまま COPY すると Lambda の
# 非 root 実行ユーザーが読めず INIT フェーズで失敗するため、build 段で正規化する。
# --chmod はディレクトリにも同じ mode が付くため、traverse に x が必要な 755 を使う
COPY --chmod=755 . .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
