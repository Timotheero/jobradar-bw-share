#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
STATE_DIR="$PROJECT_DIR/infra/firecrawl"
UPSTREAM_DIR="$STATE_DIR/upstream"
ENV_FILE="$STATE_DIR/.env"
OVERRIDE_FILE="$STATE_DIR/compose.override.yaml"
PATCH_FILES="$STATE_DIR/allow-ignore-robots.patch $STATE_DIR/search-pagination.patch"
UPSTREAM_REF=${FIRECRAWL_UPSTREAM_REF:-35d9146f93d8fe59cebe68bbbe18b5a523a0073a}

command -v git >/dev/null 2>&1 || {
  echo "Git fehlt. Bitte zuerst Git installieren." >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "Docker fehlt. Bitte Docker Engine mit Compose-Plugin installieren." >&2
  exit 1
}

if [ ! -d "$UPSTREAM_DIR/.git" ]; then
  mkdir -p "$UPSTREAM_DIR"
  git -C "$UPSTREAM_DIR" init
  git -C "$UPSTREAM_DIR" remote add origin https://github.com/firecrawl/firecrawl.git
else
  for PATCH_FILE in $PATCH_FILES; do
    if git -C "$UPSTREAM_DIR" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
      git -C "$UPSTREAM_DIR" apply --reverse "$PATCH_FILE"
    fi
  done
fi

git -C "$UPSTREAM_DIR" fetch --depth 1 origin "$UPSTREAM_REF"
git -C "$UPSTREAM_DIR" checkout --detach FETCH_HEAD

for PATCH_FILE in $PATCH_FILES; do
  if git -C "$UPSTREAM_DIR" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
    :
  elif git -C "$UPSTREAM_DIR" apply --check "$PATCH_FILE"; then
    git -C "$UPSTREAM_DIR" apply "$PATCH_FILE"
  else
    echo "Selfhost-Patch $PATCH_FILE passt nicht zum Firecrawl-Stand." >&2
    exit 2
  fi
done

if [ ! -f "$ENV_FILE" ]; then
  cp "$STATE_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Firecrawl-Konfiguration wurde als $ENV_FILE angelegt."
  echo "Bitte dort BULL_AUTH_KEY und POSTGRES_PASSWORD ersetzen und das Skript erneut starten."
  exit 2
fi

chmod 600 "$ENV_FILE"

if grep -q 'BITTE-' "$ENV_FILE"; then
  echo "In $ENV_FILE sind noch Platzhalter enthalten." >&2
  exit 2
fi

docker network inspect jobradar_sources >/dev/null 2>&1 \
  || docker network create jobradar_sources >/dev/null

(
  while IFS='=' read -r name _; do
    case "$name" in
      ""|\#*|*[!A-Za-z0-9_]*|[0-9]*) continue ;;
    esac
    unset "$name"
  done < "$ENV_FILE"

  docker compose \
    --project-directory "$UPSTREAM_DIR" \
    --env-file "$ENV_FILE" \
    -f "$UPSTREAM_DIR/docker-compose.yaml" \
    -f "$OVERRIDE_FILE" \
    up -d --build
)

echo "Firecrawl laeuft lokal auf Port 3002 und im Netz als http://firecrawl-api:3002."
