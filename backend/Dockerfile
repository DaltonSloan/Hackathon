# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

ARG INSTALL_VISION_DEPS=0

WORKDIR /app

RUN if [ "$INSTALL_VISION_DEPS" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends git \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt requirements-vision.txt requirements-torch-cpu.txt ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt \
    && if [ "$INSTALL_VISION_DEPS" = "1" ]; then \
        uv pip install --system -r requirements-torch-cpu.txt \
        && uv pip install --system -r requirements-vision.txt; \
    fi

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
