ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim as base

ARG REQUIREMENTS=tests/sampleprj/requirements.lock

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"

WORKDIR /app

RUN python -m venv $VIRTUAL_ENV
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=${REQUIREMENTS},target=${REQUIREMENTS} \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=pocket,target=pocket \
    /venv/bin/pip install -r ${REQUIREMENTS}

COPY tests/sampleprj .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
