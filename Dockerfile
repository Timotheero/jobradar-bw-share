FROM python:3.12-slim

ARG INSTALL_CODEX=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/jobradar \
    PATH=/home/jobradar/.local/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash jobradar

WORKDIR /app
COPY pyproject.toml ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /home/jobradar/.codex /data/codex-workspace \
    && chown -R jobradar:jobradar /home/jobradar /data

USER jobradar
RUN if [ "$INSTALL_CODEX" = "true" ]; then \
      curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh; \
    fi

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "jobradar.main:app", "--host", "0.0.0.0", "--port", "8080"]
