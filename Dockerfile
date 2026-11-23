FROM ghcr.io/astral-sh/uv:0.13.8 AS uv
FROM python:3.12.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV CUSTOM_TIKTOKEN_CACHE_DIR=/service/tokenizers
COPY scripts/prepare_tokenizers.py scripts/prepare_tokenizers.py
RUN .venv/bin/python scripts/prepare_tokenizers.py
COPY app app
COPY prompts prompts
COPY config config
RUN useradd --create-home triage && mkdir -p /service/state && chown triage:triage /service/state
USER triage
ARG GIT_SHA=unavailable
ENV GIT_SHA=$GIT_SHA STATE_DIR=/service/state PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True
EXPOSE 8000
CMD ["/service/.venv/bin/uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--no-access-log"]
