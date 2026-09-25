#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

command -v docker >/dev/null 2>&1 || {
  echo "Docker Engine mit Compose-Plugin ist erforderlich." >&2
  exit 1
}

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo ".env wurde angelegt. Ersetze APP_SECRET_KEY, POSTGRES_PASSWORD"
  echo "und denselben Passwortteil in DATABASE_URL. Starte das Skript danach erneut."
  exit 2
fi

chmod 600 .env

if grep -q 'BITTE-' .env; then
  echo "In .env sind noch Sicherheitsplatzhalter enthalten." >&2
  exit 2
fi
if [ -f infra/firecrawl/.env ]; then
  chmod 600 infra/firecrawl/.env
fi

docker network inspect jobradar_sources >/dev/null 2>&1 \
  || docker network create jobradar_sources >/dev/null

docker compose build
docker compose run --rm app jobradar init-db
docker compose up -d postgres app worker crawler
docker compose ps

echo "Jobradar ist auf dem Server unter http://127.0.0.1:${APP_PORT:-8080} bereit."
echo "CRAWLING_ENABLED und CODEX_ENABLED bleiben bis zur bewussten Aktivierung aus."
