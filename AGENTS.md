# AGENTS.md

WorldDB (`worlddb.online`) manages Pro Cycling Manager CDBs using Flask,
SQLAlchemy, PostgreSQL, and Celery. A Namespace contains one CDB and its workflow
data; `main` holds shared application data, while `namespace` databases are isolated.

## Code map
- `webapp/__init__.py`, `webapp/config.py`, `webapp/extensions.py`: app setup.
- `webapp/web/`: routes/forms; `webapp/templates/`, `webapp/static/`: UI.
- `webapp/models/main/`, `webapp/models/ns/`: shared and namespace models.
- `webapp/rbac_seed.py`, `webapp/security.py`: permissions and authentication helpers.
- `webapp/namespace/`: namespace provisioning and setup.
- `webapp/integrations/cdb/`: CDB import/export and active `cdb_mappings.yaml`.
- `webapp/integrations/`, `webapp/worker.py`: integrations and async work.
- `tests/`: PostgreSQL access, namespace, and CDB release regression tests.
- `docker/`, `docker-compose.yml`: local services; `.github/workflows/`: CI.
- `migrations/`: manual schema scripts; `contribution.md`: branch and PR workflow.

## Setup and verification
Run from the repository root with Docker and Docker Compose. All Python setup
and verification runs in containers; no host Python or virtual environment is needed.
Never print or commit `.env` or secrets.

```text
docker compose up -d --build
```

The app is at `http://localhost`. Compose runs `web`, `database`, `cache`,
`worker`, and `reverse-proxy`.

Run Python checks in a disposable Python 3.11 container with the repository mounted
at `/workspace`. The shell below runs inside the Linux container on every host OS:

```text
docker run --rm --mount "type=bind,source=.,target=/workspace" --workdir /workspace python:3.11-slim sh -c "pip install -r requirements-dev.txt && python -m compileall -q webapp wsgi.py tests && ruff check webapp wsgi.py tests && python -m pip_audit -r requirements.txt --strict --progress-spinner off"
docker build --file docker/webapp/Dockerfile --tag worlddb-pr-check .
```

Run regression tests against a fresh disposable PostgreSQL 15 container only,
never an existing development or production server. Use a separate Docker network
without publishing database ports. Wait for `pg_isready` to succeed before pytest:

```text
docker network create worlddb-agent-tests
docker run --detach --rm --name worlddb-agent-tests --network worlddb-agent-tests -e POSTGRES_USER=worlddb_test -e POSTGRES_PASSWORD=ci-only-password -e POSTGRES_DB=worlddb_test postgres:15-alpine
docker exec worlddb-agent-tests pg_isready -U worlddb_test -d worlddb_test
docker run --rm --network worlddb-agent-tests --mount "type=bind,source=.,target=/workspace" --workdir /workspace -e WORLDDB_TEST_DATABASE_URL=postgresql://worlddb_test:ci-only-password@worlddb-agent-tests:5432/worlddb_test python:3.11-slim sh -c "pip install -r requirements-dev.txt && python -m pytest"
```

Clean up after testing, including after failure:

```text
docker stop worlddb-agent-tests
docker network rm worlddb-agent-tests
```

Tests create temporary namespace databases and release files and deliberately do
not read `.env`. Report commands run, results, and anything unverified. For docs-only
changes, verify referenced paths/commands and run `git diff --check`.

## Protected behavior
- Enforce authentication and resource/action permissions on the server for every
  protected route, including direct requests. UI visibility is not authorization.
- Preserve edit/approval separation: `stat_editor` cannot approve stats;
  `stat_editor_approver` handles approval. Follow `rbac_seed.py` for release,
  administration, and other permissions; do not infer access from role names.
- Resolve reads and writes to the selected Namespace's database. Reject mismatched
  connections; never fall back to `main` or another Namespace. IDs can overlap
  across namespaces and must not cause cross-namespace reads or mutations.
- Before changing CDB behavior, read `webapp/integrations/cdb/cdb_mappings.yaml`.
  Preserve mapped data, untouched SQLite content, and rollback on invalid updates.
- Releases include a full CDB SQLite file, rider SQL, and overview HTML. The current
  rider SQL is a MySQL table dump, not standalone stat-update SQL; changing that
  format requires an explicit domain change.
- Add or extend focused regression tests when changing authentication, permissions,
  approval/release transitions, namespace routing/isolation, schema initialization,
  or CDB import/export. Cover allowed and denied actions, overlapping IDs in separate
  namespaces, and artifact contents/rollback as applicable. Run the PostgreSQL suite;
  if unavailable, report the gap rather than claiming verification.

## Scope and safety
- Keep changes surgical and follow existing Python/Flask patterns. No unrelated
  refactoring or dependency changes. Discuss broad architectural changes first.
- Never weaken permissions, validation, tests, lint/security checks, or CI settings
  to make a change pass. Fix the cause or report the blocker.
- Do not access production databases or execute migrations. For necessary schema
  changes, provide a manual script and document the target (`main` or `namespace`)
  and operator steps for review. Do not introduce automatic migration workflows.
- Keep worker changes limited to the requested behavior; use synthetic data and
  mocked external services in tests. Preserve secrets and namespace boundaries.
