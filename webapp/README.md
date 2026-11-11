# WorldDB Webapp

## Application configuration

Application settings live in one flat YAML file per deployment environment:

- [Local values](../envs/local/values.yaml)
- [Development values](../envs/dev/values.yaml)
- [Production values](../envs/prod/values.yaml)

Set `ENV=local`, `ENV=dev`, or `ENV=prod` in the instance environment.
[.env.example](../.env.example) includes `ENV=local`; Compose passes this to both
web and worker. If ENV is absent, local is used. Invalid selectors or missing
selected files fail instead of falling back to another environment. ENV is
never stored in the values files. `FLASK_ENV` no longer selects configuration.

[config.py](config.py) finds `envs/<ENV>/values.yaml` relative to the project root,
independent of the working directory. Each file contains the complete set of
YAML settings; no default_environment, profile nesting, or cross-file merge is
used. Unknown/missing keys, duplicate keys, invalid types and ranges fail before
application initialization. YAML settings have no environment-variable overrides.

All existing environment-backed settings are considered secret and remain
environment-only, including database URLs, Redis, Discord and administrator
settings, FC options, Activity Monitor options, authentication settings, database
echo and static asset overrides. Their keys are rejected in YAML, even with null
values. The loader reads and validates them separately without printing values.
`DATABASE_URL` maps to `SQLALCHEMY_DATABASE_URI`; missing optional environment
settings remain unset, and existing fallback values stay in Python. The loader
does not read `.env` or interpolate shell expressions. WSGI retains its existing
`.env` loading before importing the application.

Docker images include `envs/`; local Compose mounts that directory read-only
into both web and worker. Restart both services after changing values or ENV.
No database schema migration is needed for this configuration change.

`PERMANENT_SESSION_LIFETIME_DAYS` is expressed in days, `WTF_CSRF_TIME_LIMIT` in
seconds (or null), and `MAX_CONTENT_LENGTH` in bytes. `FILESTORE_ROOT` keeps the
existing behavior: relative paths resolve against the process working directory.
The main database bind always derives from `SQLALCHEMY_DATABASE_URI`; Celery's
broker/result backend derive from `REDIS_URL`, and an unset `STATIC_ASSET_VERSION`
uses `SITE_VERSION`. Namespace connections use the configured provisioner URL
with the selected Namespace database name.

## Architecture Overview

### Multi-Database Structure
- **Main Database**: Stores users, roles, permissions, and namespace registry
- **Namespace Databases**: One PostgreSQL database per CDB instance, containing riders, teams, races, palmares, photos, and stats
- **SQLite CDB Files**: PCM-compatible game database files (input/output)

### Application Workflows
- **Stats Editor**: Submit rider stat changes from race results or rider lists for validation
- **Releases**: Review changes and generate CDB release artifacts
- **Administration**: Manage users, roles, namespaces, and integrations

### User Roles

| Role | System name | Description |
| --- | --- | --- |
| Administrator | `admin` | Full system access |
| Integrations Manager | `integrations_manager` | Can view and run integrations and update Discord settings |
| Release Approver | `release_manager` | Can approve and publish releases |
| Release Editor | `release_editor` | Can create and edit releases |
| Stat Editor | `stat_editor` | Can edit rider statistics |
| Stat Editor Approver | `stat_editor_approver` | Can approve rider/team/race statistics edits |
| User Manager | `user_manager` | Can create users and update user account settings |
| Viewer | `viewer` | Read-only access to data |


```bash
docker compose down -v
```

## Discord Bot

Install the Discord bot to a guild with the required workflow notification permissions:

https://discord.com/oauth2/authorize?client_id=1505027515385647175&permissions=309237680192&integration_type=0&scope=bot

Set `DISCORD_BOT_TOKEN` in the application environment before testing or sending Discord notifications.

After the bot is installed, go to **Admin > Integrations > Discord** for the target namespace and configure:

- `Channel ID`: Discord channel where workflow threads/messages should be created
- `Approver Role ID`: Discord role to mention for approval notifications

Use the **Test** action in the Discord integration settings to confirm the bot can create a thread and post in the configured channel.

## External call queue

Under **Riders → Bulk Tasks**, use **Groups** to view namespace task groups and
filter by type. Use **Editor** to create a draft, select FC Rider or PCS Rider,
and add or remove namespace riders. The rider selection actions also create
drafts. **Queue group** validates the current rider links and submits the saved
group. Only drafts can be edited. **Cancel** stops pending requests in draft,
queued, or running rider groups. An HTTP request already in progress may finish
and save its result, while the group retains its cancelled status.

The existing group and task status columns store draft and cancelled states.
This lifecycle change requires no database schema migration. Drafts are excluded
from dispatch. Group row locks serialize membership edits, queueing, task claims,
completion, and cancellation.

`ExternalCallTaskGroup` and `ExternalCallTask` use the **main** database. A group
may mix providers and optionally references a Namespace through `namespace_id`.
Deleting a Namespace clears that reference and preserves the group and requests.
Namespace context does not select a database for fetching or authorize access.
Any future user-facing enqueue route must enforce Namespace permissions itself.
No user-facing enqueue endpoint or bulk PCS UI is added in this foundation.

Tasks optionally retain `related_to_id` (integer) and `related_to_table` (up to
100 characters). For a Namespace rider, use `related_to_table="rider"` and the
Namespace's rider ID, with `namespace_id` on the group. These are informational
references, not foreign keys or instructions to dynamically query a table.
They may remain null for unassociated calls. A future rider history lookup must
join the group and filter its Namespace as well as both reference fields, since
rider IDs can overlap across Namespaces. Fetch success/failure remains on the
task; no Rider page or history endpoint is configured here. If the group's
Namespace is deleted, the surviving reference must not be resolved elsewhere.

The service API creates a group atomically after validating every request:

```python
from webapp.worker import create_manual_external_group

group = create_manual_external_group([
    {"provider": "pcs", "endpoint": "get_rider_profile",
     "params": {"rider_slug": "example-rider"},
     "related_to_table": "rider", "related_to_id": selected_rider.id},
    {"provider": "firstcycling", "endpoint": "get_rider_profile",
     "params": {"rider_id": 123}},
], namespace_id=selected_namespace.id, executed_by_id=current_user.id)
# The helper commits the group and records, then submits its Celery message.
```

Provider handling, queue services, and Celery entry points live together in
[worker.py](worker.py).

Run the single combined Celery worker/Beat service with concurrency and prefetch
both set to one. Redis holds actual Celery messages separately from the database
backlog. Manual creation commits a high-priority group and publishes immediately;
a broker failure leaves it in the database for recovery by the next heartbeat.
Beat dispatches database groups hourly (`EXTERNAL_CALL_POLL_SECONDS: 3600`).
It fills the remaining low-priority waiting slots, oldest timestamp then ID first.
Unsubmitted manual groups are also recovered, without the low-priority cap.
Submission uses a PostgreSQL lock to serialize the cap check and publication.
`is_worker_queued` distinguishes groups already sent to Celery from database-only
groups; `worker_message_id` guards against obsolete or duplicate deliveries.

The worker consumes control jobs (`celery`), high groups (`external.high`), then
low groups (`external.low`) with Redis's sorted queue ordering. Groups within
each queue execute in publication order. Late acknowledgement on group tasks and
prefetch one keep later groups in Redis while the current group runs. Starting
a group changes its status to `running` and clears `is_worker_queued`. It finishes
the entire group, including PCS spacing and retry waits, before taking another
group; high priority never interrupts an active group. FirstCycling tasks within
that group may proceed during a PCS wait. Empty waiting slots are filled only
on the next heartbeat, not on group completion.

The Redis Python client is pinned to 6.4.0 for compatibility with the Kombu
transport installed by Celery. Queue tests include an optional real broker check:
provide `WORLDDB_TEST_REDIS_URL=redis://worlddb-agent-cache:6379/0` pointing to a
fresh disposable Redis 7 container on the isolated test network. Never point this
check at a development or production broker.

Tasks use PostgreSQL row locks to prevent duplicate execution. A shared execution
lock also prevents two group runners from overlapping. PCS uses a shared advisory lock and persisted
state to allow one active request across all workers. Starts are spaced by at
least `PCS_REQUEST_DELAY_SECONDS` (Flask config, default 5, minimum 1 second).
PCS 403/429/5xx responses impose a provider-wide cooldown of at least 60 seconds,
honoring both forms of Retry-After even when the last attempt fails. FC requests
have no spacing delay and use their own retry policy. PCS tasks make at most
two attempts (one retry), while FC tasks make at most three attempts. PCS HTTP 200
responses are not fetched again when parsing or saving the results fails. These
processing failures finish the task with an error. The group worker waits for
retries while retaining ownership of its group. Interrupted group deliveries can
resume through Celery redelivery;
requests left `running` are marked failed for review rather than fetched again.

`enqueue_group` defaults to `origin="manual"` and `priority="high"`. Future
scheduled producers should pass `origin="scheduled"`, which defaults to low
priority. Explicit priority overrides accept only `high` or `low`. Producers must
commit the group and its tasks together. Application actions should use
`create_manual_external_group`, which commits and immediately submits; lower-level
`enqueue_group` only stages database records and never publishes uncommitted work.

The three environment profiles in `envs/<ENV>/values.yaml` contain:

```yaml
EXTERNAL_CALL_POLL_SECONDS: 3600
PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE: 30
PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED: 5
```

The two limits are positive integers. The first limits riders per scheduled PCS
group; the second limits low-priority groups submitted but not yet running. High
groups and the active group do not count toward that cap. These settings use the
existing YAML configuration system, not environment-variable overrides.

Future scheduled selection calls `enqueue_scheduled_pcs_groups(requests, namespace_id=...)`
with one validated PCS profile request per distinct selected rider. It creates as
many low groups as needed, including a final partial batch, without submitting
them to Celery. The scheduled process must first generate and commit those groups,
then call `dispatch_queued_external_groups()` to fill available waiting slots.
There is no limit on database-only backlog. The hourly heartbeat currently runs
the dispatch step only: rider-selection SQL, next-check dates and the bulk
selection UI are still future work. Activity Monitor's
existing direct collectors and legacy FC sync jobs still use their own workflow;
the shared queue's PCS gate does not govern those requests.

For existing queue deployments, an operator must review and apply
[external_call_group_priority_main.sql](tools/external_call_group_priority_main.sql)
to **main**, after the original queue migration and before deploying this code.
Stop web and the combined worker/Beat, back up and confirm main, apply with
`psql -v ON_ERROR_STOP=1`, review pending groups (defaulted to manual/high), then
restart the services. Drain or remove old per-request/drain messages before
switching versions; the old per-request entry point intentionally does no work.
No schema scripts are applied automatically. Do not clear Redis queues independently
of submission metadata: groups marked `is_worker_queued` are not republished by
the heartbeat. An operator must reconcile affected groups after broker data loss.

PCS supports only `get_rider_profile`, fetching a validated canonical slug's
`/road` page with redirects disabled. The independent parser preserves profile
facts and result rows. Parsed responses are retained in the task's `result_json`;
this does not yet upsert shared PCS rider/result tables or match Namespace riders.
Failures remain explicit, including blocked or unrecognized HTML. Interrupted
running requests become failed for operator review, rather than silently fetching
again. Groups aggregate queued/running/retry, success, failed and partial outcomes.

Existing FC sync routes keep their original behavior through compatibility model
aliases. The group `provider` and `sync_type` columns are nullable legacy audit
metadata; new queue groups do not assign a provider. Historical API logs have
`status=historical` and never execute. FC bulk syncs still run through their
existing workflow; they are not expanded into queue tasks by this change.

### Existing deployments: manual main schema update

Review [external_call_queue_main.sql](tools/external_call_queue_main.sql).
Back up main and stop web, workers and scheduler. Confirm the selected database
is **main**, then have an operator run from the repository root:

```text
psql -v ON_ERROR_STOP=1 -d <main-database> -f webapp/tools/external_call_queue_main.sql
```

The script renames both existing tables and their group FK column, preserves
history, adds the optional Namespace FK and queue fields, and runs transactionally.
It intentionally fails if already applied. No Namespace schema changes are needed.
Deploy matching code before restarting services; old code cannot use renamed tables.
No migration is applied by the queue. New installations use the existing model
initialization process.

For optional record associations, also review and manually apply
[external_call_task_related_record_main.sql](tools/external_call_task_related_record_main.sql)
to **main**, after the queue migration (or directly if that migration was already
applied), with services stopped:

```text
psql -v ON_ERROR_STOP=1 -d <main-database> -f webapp/tools/external_call_task_related_record_main.sql
```

Existing tasks retain null references; no backfill or Namespace schema change
is performed. This script has not been applied by the application.

To roll back, stop services and export new queue records/results first. Restore
the pre-change main backup and matching application version together. Renaming
back and dropping columns alone would lose queue data and cannot safely restore
mixed-provider groups to the old non-null provider schema.

### External sync scheduler

For an existing deployment, back up and verify the **main** database, then have
an operator review and apply [scheduled_jobs_main.sql](tools/scheduled_jobs_main.sql):

```text
psql -v ON_ERROR_STOP=1 -d <main-database> -f webapp/tools/scheduled_jobs_main.sql
```

PCS rider storage must already be installed in **main** via
[pcs_rider_tables_main.sql](tools/pcs_rider_tables_main.sql). Back up each
**namespace** database and pause writes before applying
[rider_checks_namespace.sql](../migrations/rider_checks_namespace.sql).
Apply the preceding rider PCS slug migration first if required. Then apply
[rider_pcs_birth_date_namespace.sql](../migrations/rider_pcs_birth_date_namespace.sql)
once per existing namespace to add the PCS date and drop the old match flag.
New namespaces get the date column during provisioning.

To recover existing dates before dropping the flag, pause web and worker
writes. In a separate `psql` session connected to **main**, export the stored
PCS facts to a local file:

```sql
\copy (SELECT rider_slug, birth_date, last_synced_at FROM pcs_riders WHERE birth_date IS NOT NULL) TO 'pcs-birth-dates.csv' WITH (FORMAT csv)
```

In a `psql` session explicitly connected to the **namespace**, run the following
from the repository root after the preceding namespace migrations. Keep the
temporary table and migration in the same session.

```sql
\set ON_ERROR_STOP on
CREATE TEMP TABLE pcs_birth_date_backfill (
    rider_slug TEXT PRIMARY KEY, birth_date DATE NOT NULL, last_synced_at TIMESTAMP NOT NULL
);
\copy pcs_birth_date_backfill FROM 'pcs-birth-dates.csv' WITH (FORMAT csv)
\i migrations/rider_pcs_birth_date_namespace.sql
```

The backfill uses the slug and exact check timestamp to tie each date to its
namespace rider. You can run the migration without the temporary table if the
export is unavailable. Existing verified or review records without a recovered
date return to Unknown so an identity check can fetch the date again. Remove
the local CSV after migrating all namespaces. Deploy the matching code and
restart web and the Celery worker with Beat.

Create named jobs under **Settings → External Data Integrations → Scheduler**.
Each enabled job runs at most once per day at or after its configured time in
`WORKER_TIMEZONE`. The previous `external_sync_schedules` table is retained
for reference. Recreate any schedules you want to keep as named jobs.

- **Race Results** queues a global FirstCycling race results group.
- **Rider Identity Check** selects up to the configured number of riders from
  one namespace whose PCS identity is unknown. It queues one PCS task group
  using the riders' current slugs.
- **Rider Activity Check** checks one selected rider through separate PCS and
  FirstCycling groups and can skip recent FirstCycling race results.
- Both rider jobs can use a minimum time since the last check. **Run Now**
  uses the current form settings.

Successful PCS requests save shared PCS profile and result data. A linked
rider gets `verified` for matching birth dates, `review` for missing or
different dates, or `failed` on a terminal fetch failure. The DOB match is
unknown when either date is missing. The override stays separate from the
fetch result and is editable through rider attribute approval. A changed
slug clears the old identity evidence and override. FC Rider tasks refresh
the shared FirstCycling profile linked by `first_cycling_id`.
