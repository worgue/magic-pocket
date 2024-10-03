ARG PYTHON_VERSION=3.12
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS base

FROM base AS builder
ARG REQUIREMENTS=requirements.lock
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app

# install git
RUN apt-get update && apt-get install -y git

RUN python -m venv $VIRTUAL_ENV
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=${REQUIREMENTS},target=${REQUIREMENTS} \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    /venv/bin/pip install -r ${REQUIREMENTS}

FROM base AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app
COPY --from=builder /venv /venv
COPY . .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
