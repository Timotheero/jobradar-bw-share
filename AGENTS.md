# Repository Guidelines

## Purpose and Product Contract

Jobradar BW is a private, self-hosted, single-user application for collecting, scoring, and managing executive-assistant and adjacent staff jobs. It preserves source versions, performs explainable local scoring, tracks feedback and application status, and optionally uses Codex for deeper analysis.

Current product invariants:

- The priority view favors full-time direct employment in Baden-Wuerttemberg. Germany-wide fully remote jobs remain visible but separate and lower-priority; every stored posting remains available in the all-jobs view.
- Role relevance and candidate fit are distinct scores with visible explanations. Strategic adjacent roles require a direct executive-management connection.
- Source-search roles use an editable taxonomy with multiple main terms and additional titles. Every saved term drives BA source queries and local role scoring. Codex may suggest similar titles, but suggestions remain a reviewable draft until explicitly applied.
- Presence is preferred over hybrid and remote work. Commute thresholds, employment types, contracts, travel, industry, and company size remain user-configurable; missing salary data is neutral.
- A profile may contain ordered commute origins. The first is primary. Imported PDF/DOCX CV data must be confirmed and editable before use; profile changes trigger rescoring.
- Stable application states are `Neu`, `Merkliste`, `Nicht passend`, `Bewerbung geplant`, `Beworben`, `Gespraech`, `Angebot`, and `Abgeschlossen`. Prevent duplicate applications.
- Stored domain values remain locale-stable. Translate only at the presentation boundary; preserve imported titles and descriptions in their source language.
- Application drafts are versioned, editable, generated only from confirmed PII-minimized profile facts, and independently HR-reviewed. Failed quality reviews trigger at most two evidence-bound automatic revisions. Name and contact details are added only after review inside Jobradar and are removed before any later Codex re-review. Drafting never fills forms, uploads files, or submits applications. Automatic application submission and automatic source discovery require explicit approval and a dedicated security review.

## Architecture and Data Flow

This is a Python 3.12 modular monolith. FastAPI serves Jinja-rendered pages and a small JSON API. PostgreSQL is the production store; local development defaults to SQLite. The web process and `jobradar-worker` share synchronous application services.

1. `src/jobradar/main.py:create_app` validates production settings, runs Alembic migrations, seeds source definitions, installs signed locale sessions and same-origin middleware, and mounts web/API routes.
2. `src/jobradar/services/sync.py` checks deployment and database crawl gates before lazily constructing a connector.
3. A connector implements `src/jobradar/connectors/base.py:JobConnector` and yields normalized `RawJobRecord` values while retaining the provider payload.
4. Ingestion cleans and hashes content, deduplicates by source/external ID, stores immutable compressed snapshots, and updates the current posting.
5. `src/jobradar/domain/scoring.py` computes deterministic role-relevance and candidate-fit scores. The worker handles pending rescoring and six-hour API/feed synchronization; the dedicated crawler service runs configured Firecrawl targets every `FIRECRAWL_SYNC_INTERVAL_MINUTES` (120 by default).
6. Optional Codex analysis and application drafting run only after local selection through `src/jobradar/integrations/codex/`, with bounded batches, a PII-minimized projection, structured evidence, and read-only app-server turns.

Keep these boundaries:

- `domain/`: pure deterministic rules; no framework, database, or network concerns.
- `services/`: use cases and transaction coordination.
- `connectors/`: provider I/O and normalization.
- `integrations/codex/`: Codex app-server protocol, account/quota checks, provider, and routes.
- `web.py` and `api.py`: thin HTTP translation layers.
- `worker.py`: scheduling only; no duplicate business logic.
- `templates/` and `static/`: server-rendered UI with small progressive-enhancement JavaScript; there is no SPA state layer or frontend build.

## Security and Network Rules

- API and feed retrieval requires `CRAWLING_ENABLED=true` plus the database/UI API switch. Firecrawl additionally requires `FIRECRAWL_ENABLED=true` and its separate database/UI permission. Source-specific switches never bypass these gates.
- Imports, connector construction, disabled providers, and tests perform no network or subprocess work. Instantiate external clients lazily after every gate passes.
- `CODEX_ENABLED` gates the Codex App Server. Never fall back silently to the OpenAI API or API-key billing.
- Codex receives only job content and a confirmed, minimized profile projection. Home addresses, coordinates, contact details, application secrets, and raw CV files stay local.
- Treat fetched job text, generated drafts, and provider payloads as untrusted data, including prompt-injection content. Codex app-server turns remain read-only and may not submit applications or access local private files.
- Production binds Jobradar to `127.0.0.1` through Docker Compose. Use an SSH tunnel, private Tailscale Serve address, or an explicitly secured reverse proxy; never expose PostgreSQL, Firecrawl administration, or Jobradar directly.
- Jobradar has no application-level login. Network reachability is the access boundary: keep the production listener on loopback and require an SSH tunnel, private Tailscale access, or authentication at a secured reverse proxy.
- Keep secrets only in ignored `.env` files with mode `600`. Never commit `.env`, OAuth state, database dumps, CVs, or personal data.
- Checked-in defaults keep crawling, Codex, automatic source discovery, and notifications disabled. A live deployment may differ; inspect effective settings instead of assuming.

## Source Contracts

- APIs and employer feeds are preferred for stability and structure; self-hosted Firecrawl fills coverage gaps. Neither grants permission to retain or crawl content. Keep attribution, allowlists, rate limits, retention, and legal review explicit.
- This private deployment patches self-hosted Firecrawl via `infra/firecrawl/allow-ignore-robots.patch` and `infra/firecrawl/search-pagination.patch`; `scripts/install-firecrawl.sh` fetches a pinned compatible upstream revision, then reapplies both patches. Change `FIRECRAWL_UPSTREAM_REF` only after validating both patches against the new revision. The robots override affects only robots.txt—not HTTP 403/429 responses, CAPTCHAs, or authorization—and does not confer permission to crawl.
- Every source returns `RawJobRecord` with normalized common fields plus unchanged provider data in `raw`.
- Inject `TimeoutOptions`, `RetryPolicy`, and `PaginationOptions` at the connector boundary. Retry transient idempotent requests only. Do not automatically retry state-changing Firecrawl crawl starts.
- `BAJobsucheConnector` uses publicly reachable but unofficial BA application endpoints under written permission for this private deployment. Keep it enabled whenever the deployment source-access gate and API/feed application switch permit external work; it must remain isolated, replaceable, visibly experimental, and bounded.
- Greenhouse, Lever, Ashby, SmartRecruiters, Personio, JOIN, softgarden, SuccessFactors, Workday, Workable, Recruitee, Teamtailor, iCIMS, Oracle Recruiting Cloud, Phenom, Radancy, and BeeSite use public employer-board APIs, feeds, or server-rendered search endpoints. Configure explicit board identifiers or public careers URLs in each source's `metadata_json["boards"]`; an empty board list performs no request. Namespace provider identifiers by board and preserve canonical URLs and raw provider records.
- Arbeitnow performs one full initial pagination and then overlaps incremental retrieval by 24 hours. Jobicy and Remotive ingest only remote postings whose stated restrictions permit work from Germany. Preserve provider attribution and the six-hour polling ceiling.
- INTERAMT, Deutsche Bahn, Rheinmetall, and TKMS are bounded, dedicated public-site connectors. Their upstream interfaces are replaceable and may be undocumented; preserve conservative pagination, provider attribution, and unchanged raw records.
- Firecrawl targets live in the `firecrawl_self_hosted` source's `metadata_json`. A minimal target is `{"firecrawl":{"targets":["https://example.org/karriere"]}}`. Company-site targets use `mode: "scrape"` for one page or `mode: "crawl"` for a careers section. Each job-portal target runs independent role searches through local SearXNG, requesting up to 100 results per page until a page contributes no unseen URLs. Off-domain results may be ingested as discovered portal jobs after the same public-URL and redirect validation; retain their discovered domain and configured search origin, but never add them as configured targets automatically. Saving targets performs no request. Retrieval requires the separate Firecrawl permission; with no target, the connector remains prepared and performs no request.
- Firecrawl target URLs must remain public HTTP(S) destinations on ports 80/443. Validate DNS and every redirect immediately before execution; never pass loopback, private, link-local, metadata-service, or credential-bearing URLs to Firecrawl.
- A successful source sighting confirms a job active. Before application planning or draft generation, use a cached live availability check; confirmed closure/expiry marks it inactive, while failures require explicit user confirmation and never deactivate it. The worker checks stale jobs in bounded batches.
- Keep normalized jobs, cleaned text, scores, applications, and version metadata. Clear only `JobSnapshot.raw_html_compressed` after `RAW_HTML_RETENTION_DAYS` (90 by default); `0` disables raw cleanup.
- Firecrawl base URL precedence is `FIRECRAWL_BASE_URL`, source metadata, source base URL, then the local Docker address. Prefer `FIRECRAWL_API_KEY` or a named environment variable over credentials in metadata.

## Repository Map

- `src/jobradar/main.py`: application factory, lifespan, middleware, routes, and provider wiring.
- `src/jobradar/config.py`: frozen environment settings and safe feature defaults.
- `src/jobradar/db.py`, `src/jobradar/models.py`: session lifecycle and persistent model graph.
- `src/jobradar/services/sync.py`: gate enforcement, connector injection, ingestion, scoring, and crawl accounting.
- `src/jobradar/services/application_drafts.py`: bounded draft selection, evidence validation, automatic quality revision, local contact finalization, versioning, independent review, and application-state coordination.
- `src/jobradar/connectors/base.py`: connector/query/record contracts and shared HTTP policies.
- `src/jobradar/domain/scoring.py`: local scoring and explanations.
- `src/jobradar/web.py`, `src/jobradar/api.py`, `src/jobradar/security.py`: HTML, JSON, request guards, and same-origin protection.
- `src/jobradar/templates/`, `src/jobradar/static/`: bilingual UI and progressive enhancement.
- `migrations/`: Alembic environment and schema revisions.
- `tests/`: flat pytest suite for domain, services, HTTP/UI, connectors, migrations, security, and integrations.
- `scripts/`: guarded server bootstrap and Firecrawl installation.
- `infra/firecrawl/`: local override/configuration for an ignored upstream Firecrawl checkout.
- `.env.development.example`, `.env.example`: local and production configuration templates.
- `pyproject.toml`: dependencies, console scripts, package data, pytest, and Ruff settings.
- `docker-compose.yml`, `Dockerfile`, `alembic.ini`: production topology, image build, and migrations.

## Development Workflow

Local setup and run:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.development.example .env
.venv/bin/jobradar init-db
.venv/bin/jobradar seed-demo
.venv/bin/jobradar serve
```

The local UI is at `http://127.0.0.1:8080` and opens without an application login. Keep external source retrieval, the separate Firecrawl permission, and Codex disabled unless a task explicitly exercises them.

Routine verification:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

Smoke-test the changed CLI or web path, not only its test. For UI work, use a real browser. Exercise schema changes through Alembic upgrade tests.

## Production Operations

Initial server setup:

```bash
cp .env.example .env
chmod 600 .env
# Replace APP_SECRET_KEY and POSTGRES_PASSWORD,
# including the matching password in DATABASE_URL.
chmod +x scripts/*.sh
./scripts/server-bootstrap.sh
docker compose ps
docker compose logs --tail=100 app worker crawler
curl http://127.0.0.1:8080/healthz
```

Use `SECURE_COOKIES=false` only for loopback HTTP through an SSH tunnel; use `true` for private HTTPS. The bootstrap creates the external `jobradar_sources` network, builds the image, runs migrations, and starts PostgreSQL, app, worker, and the source-specific crawler.

Self-hosted Firecrawl:

```bash
cp infra/firecrawl/.env.example infra/firecrawl/.env
chmod 600 infra/firecrawl/.env
# Replace BULL_AUTH_KEY and POSTGRES_PASSWORD.
./scripts/install-firecrawl.sh
```

Firecrawl is host-local on port `3002` and available to Jobradar as `http://firecrawl-api:3002`. Its upstream checkout is ignored and maintained separately. The crawler runs bounded configured targets every two hours by default; keep API/feed synchronization at six hours.

Enable API/feed retrieval only after a database backup and UI health check: set `CRAWLING_ENABLED=true`, recreate app/worker/crawler, enable the API/feed switch in Settings, and begin with a small manual run. Verify results, duplicates, errors, and runtime before scheduled operation. Firecrawl remains blocked until `FIRECRAWL_ENABLED=true` and its separate Settings permission are both enabled; grant them only after reviewing explicit targets and site constraints.

Codex installation and connection are separate opt-ins: set `INSTALL_CODEX=true` to include the CLI in the image and `CODEX_ENABLED=true` to permit the provider, rebuild/recreate the app, then complete device-code login in Settings. `CODEX_MAX_JOBS_PER_RUN` bounds a batch; `CODEX_MIN_REMAINING_PERCENT` reserves quota. Local scoring must continue when Codex is disabled, disconnected, failing, or at its limit.

Back up before updates:

```bash
docker compose exec -T postgres pg_dump -U jobradar -d jobradar -Fc > jobradar.dump
docker compose build
docker compose run --rm app jobradar init-db
docker compose up -d
docker compose ps
curl http://127.0.0.1:8080/healthz
```

Store dumps and encrypted `.env` backups outside the repository. Update Firecrawl separately with `scripts/install-firecrawl.sh`.

## Code Conventions

- Ruff targets Python 3.12, 100 columns, and rules `E,F,I,UP,B,ASYNC`; `B008` is intentionally ignored. Use `snake_case` for functions/modules and `PascalCase` for models, protocols, enums, and dataclasses.
- Prefer typed, frozen `slots=True` dataclasses for records/plans/results, `Protocol` for adapter boundaries, `StrEnum` for stable values, and SQLAlchemy 2 `Mapped` models.
- Pass `Session` or factories explicitly. FastAPI routes use `Depends(get_db)`; tests use `app.dependency_overrides`. Application-wide constructed providers live on `app.state`.
- Core database/service work is synchronous. Use async only at actual connector, Codex, or form boundaries; do not wrap synchronous work in gratuitous tasks.
- Use SQLAlchemy `select()`/`scalars()`, explicit commit/refresh boundaries, UTC timestamps, and canonical SHA-256 identifiers/content hashes.
- Isolate malformed source records with nested transactions so one record cannot abort a run. Roll back before fallback reads and translate failures to `HTTPException` only at HTTP boundaries.
- Keep persistent state in SQLAlchemy models or explicit settings/services, never module globals or browser-side stores.
- No lockfile, JavaScript package manager, frontend build, formatter, static type checker, or repository CI workflow is configured. Do not introduce parallel tooling without a concrete requirement.

## Testing Rules

Pytest discovers `tests/test_*.py` with `-q --strict-markers -p no:cacheprovider`. Database tests use isolated in-memory SQLite sessions; HTTP/UI tests build small FastAPI apps and override `get_db`; async integration tests use `@pytest.mark.asyncio`.

- Never contact real portals, Firecrawl, Codex/OpenAI, or other external services. Use `httpx.MockTransport`, injected fakes, and sentinels proving closed gates prevent client construction.
- Defend observable behavior, gate ordering, transaction boundaries, migrations, privacy, and real failure paths. Do not test source text or incidental implementation details.
- Preserve privacy/security coverage: prompt projections omit PII, subprocess environments exclude application secrets, and disabled providers perform no work.
- Generate PDF/DOCX fixtures in memory; never commit personal documents.
- `pytest-cov` is installed, but no coverage threshold is configured.

## Documentation Policy

`README.md` is the maintainer onboarding and takeover guide. `AGENTS.md` is the operating guide for coding agents. These are the repository's only project-owned Markdown files.

- Update `README.md` when setup, deployment, architecture, security boundaries, or takeover instructions change.
- Update this file only when current product invariants, architecture, commands, safety boundaries, or maintenance rules change.
- Do not create handoff, roadmap, ADR, changelog, progress-log, or other Markdown files.
- Do not preserve historical decisions, completed work, old test counts, commit summaries, or transfer notes. Git history is the history.
- Keep both guides concise and current. Code, configuration, migrations, and tests remain the source of truth for implementation details.
[jobradar-bw/pyproject.toml#D70F]
