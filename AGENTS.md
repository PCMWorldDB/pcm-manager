# AGENTS.md

## Project Overview
WorldDB is a web app hosted at `worlddb.online` for managing Pro Cycling Manager data.

The app manages game database content stored as a `CDB`, along with app-specific workflow for editing, validation, release management, and namespace administration.

## Core Domain Terms
- `CDB`: the Pro Cycling Manager database containing riders, races, teams, stats, and related game data.
- `Namespace`: a maintained instance of a single CDB plus namespace-specific application data.
- `Main database`: shared application data used across the app.
- `Namespace database`: per-namespace data tied to a specific CDB instance and related namespace workflow.

## Stack
- Backend: Python
- Web framework: Flask
- ORM: SQLAlchemy
- Database: PostgreSQL
- Local development: Docker
- Deployment: Podman on a home lab host
- Services: `web`, `db`, `cache`, `worker`

## Data Model Guidance
- SQLAlchemy models are defined in the `models/` area of the codebase.
- The app uses two database types:
  - `main`: shared webapp data and cross-namespace app data
  - `namespace`: data specific to an individual namespace/CDB instance
- The CDB structures used by the app are defined in `webapp/models/ns/cdb_mappings.yaml`.
- When working on CDB-related features, preserve alignment with `webapp/models/ns/cdb_mappings.yaml`.

## Primary User Roles
- `editor`: edits database objects such as Teams, Riders, Stats, and related records
- `validator`: reviews and approves changes for certain database tables, such as rider stats
- `release_manager`: generates CDBs and distributes releases externally
- `admin`: creates new databases and namespaces, manages users, and has full access

## Release Artifacts
- Releases are a set of files.
- A release should include:
  - the CDB database as a SQLite file containing the full CDB with all included edits
  - a rider stats update SQL file containing standalone SQL to update rider stats without replacing the full CDB
  - a release overview HTML file

## Change Priorities
Prefer correctness of CDB-related data, role workflow, and namespace behavior over cleanup or large-scale redesign.

## Schema Changes
- Use care when changing database schema.
- Prefer manual ad hoc migration execution for now.
- Do not introduce automatic migration workflows unless explicitly requested.

## Refactoring Rules
- Keep changes surgical by default.
- If a broad refactor seems beneficial, discuss it before proceeding.

## Worker Guidance
- The `worker` service handles asynchronous tasks.
- Current worker responsibilities include fetching tasks from an external API into application databases.
- Avoid changing worker behavior broadly unless the task requires it.

## Sensitive Areas
- Treat `.env` and secrets as sensitive.
- Be careful with role and permission logic, namespace boundaries, CDB generation, and schema changes.

## Coding Guidance
- Follow normal Python PEP-style conventions.
- Use existing patterns in the codebase.
- Prefer extending current structures over introducing new abstractions unless clearly justified.

## Testing and Verification
- There is no formal testing pattern yet.
- Use lightweight validation appropriate to the change and clearly report what was verified and what was not.
- If adding tests, keep them focused and consistent with the existing project structure.

## Preferred Agent Behavior
- Ask before making broad architectural changes.
- State assumptions clearly when behavior is ambiguous.
- Preserve domain terminology such as `CDB`, `Namespace`, `main`, and `namespace`.
- For CDB-related work, check `webapp/models/ns/cdb_mappings.yaml` before making assumptions about available structures.
