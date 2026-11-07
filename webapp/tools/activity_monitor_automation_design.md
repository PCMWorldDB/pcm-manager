# Activity Monitor automatic checks

Automatic checks are opt-in and saved per namespace in
`activity_monitor_schedules`. A missing schedule row or `enabled = false` means
no automatic run is queued. Saving settings does not edit imported rider or
research history.

## Scheduling behavior

- Celery Beat polls once per minute. The local Compose worker runs Beat inside
  the worker service with `celery -A webapp.worker:celery worker --beat
  --concurrency=1 --prefetch-multiplier=1 --loglevel=info
  --schedule=/tmp/celerybeat-schedule`.
  This existing Activity Monitor poll stays at one minute; the separate shared
  TaskGroup dispatcher runs hourly and does not yet generate rider-selection SQL.
- The planner checks one enabled schedule at a time under a database row lock.
  It reads the namespace database and selects currently due riders using the
  same planning function as the manual due list.
- Manual and automatic rounds cannot overlap within a namespace. If a round is
  already queued, running, or waiting for retry, the planned automatic round is
  skipped; later planned rounds remain available.
- A round selects up to 25 riders, due first and then by the next scheduled
  research date, with WorldDB rider ID as the tie-breaker. Riders with any
  source check already recorded on the current Amsterdam day are skipped. The
  configured 1–5 rounds per hour are placed at
  stable random times for that day, at least 10 minutes apart. A final partial
  hour gets a proportional number of rounds where spacing permits. Missed
  rounds are skipped, not caught up. Scheduled rider requests use the saved
  fixed pause, which defaults to five seconds.
- A round is queued only when PCS is enabled on the server. The planner uses
  FirstCycling too when it is enabled; otherwise it uses PCS only. Existing
  eligibility, identity, verified activity, source-link, completed-research,
  retry, and source-cycle gates still apply.
- Scheduling only creates queue records. The existing worker performs source
  requests and records their outcomes. Celery Beat resumes queued work and
  retries whose wait has ended when a run has made no progress for five
  minutes. Source-setup waits resume automatically once every required source
  is enabled. If a worker stops mid-check, the next runner requeues that task
  through the normal history-recording path after it obtains the run lock. A
  queued round advances the next-run time before requests begin.

## Deployment and operator notes

The automatic schedule is intentionally disabled by default. Before enabling it
in a namespace, confirm that the namespace has the Activity Monitor tables,
PCS is enabled for the server, and the combined Celery worker/Beat service is
running. Run exactly one instance of this combined service to avoid duplicate
periodic jobs. If multiple workers are needed later, move Beat back into a
single separate service and remove `--beat` from every worker command.

When upgrading an existing Compose deployment, stop and remove the old scheduler
before starting the combined worker:

```text
docker compose stop scheduler
docker compose rm -f scheduler
docker compose up -d --build worker
```

Run the first two commands using the old Compose configuration, before replacing
it with the combined-service configuration. Worker restarts also restart Beat;
its local schedule file is temporary and is lost when the container is recreated.
The current cloud release is not changed by this configuration change. Cloud
deployments must likewise run one combined worker/Beat service or one separate
Beat instance; without Beat, saved settings will not be polled. No production
database is changed by this configuration change. Saving a disabled setting stores only
the schedule configuration in the selected namespace; it does not initiate
external source requests.
