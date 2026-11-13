# ProCyclingStats (PCS)

## Shared rider storage

`PcsRider` (`pcs_riders`) and `PcsRiderResult` (`pcs_rider_results`) use the
`main` bind and store PCS rider-page data independently of the Activity Monitor,
FirstCycling IDs, and Namespace CDB rider matches. The existing `PcsRaceResult`
race-page matching workflow is unchanged. The external call worker writes the
profile and result rows after a successful PCS Rider request.

The rider's canonical PCS slug is unique; `profile_url`, `display_name`,
`birth_date`, and `nationality` hold profile facts. `source_payload` retains
additional parser facts such as title, heading, and canonical URL. Only facts
currently extracted are modeled; height, weight, and team are not extracted by
the current rider-page parser.

The profile upsert also stores the response's `latest_participation` in nullable
`latest_participation_*` columns. These include year, date, race name/category/type,
race ID/URL, stage number/name, row type, result position, participation status,
and the confirmed-participation flag. The payload's `result` maps to
`latest_participation_result_position`, `participation_status` to
`latest_participation_status`, and `confirmed_participation` to
`latest_participation_confirmed`. A nonempty latest participation replaces the
saved summary. Responses with missing, null, or empty latest participation and
failed requests preserve the previous summary.

For an existing `pcs_riders` table, back up **main**, pause web and worker writes,
confirm the database with `SELECT current_database()`, and have an operator review
and apply [pcs_rider_latest_participation_main.sql](../../tools/pcs_rider_latest_participation_main.sql)
before deploying the matching code and restarting web and workers.

```text
psql -v ON_ERROR_STOP=1 -d <main-database> -f webapp/tools/pcs_rider_latest_participation_main.sql
```

Existing rows receive null summary values until a successful PCS fetch includes
a nonempty latest participation.
Fresh installations include these columns in the model and table creation script.

Result columns follow `profile.parse_pcs_profile` rows: `year` maps to
`season`, `date` to `result_date`, and `result` to `result_position`. Other
fields retain their parser names. `race_id` is a PCS string grouping key, not
a numeric FirstCycling ID. `source_payload` retains the original row. Missing
season/date values are allowed, and headings, classifications, DNS, and DNF
remain distinguishable. `confirmed_participation` is the parser's row flag,
not confirmation that a PCS profile matches a CDB rider.

The worker builds a stable `source_result_key` from the race URL, date, row
type, and occurrence within rows sharing those fields. The database enforces
uniqueness of `(rider_id, source_result_key)` even
for undated rows. Deleting a PCS rider cascades to that rider's results.
Neither table references monitor records or Namespace rider IDs.

For an existing deployment, an operator must review and manually apply
[`pcs_rider_tables_main.sql`](../../tools/pcs_rider_tables_main.sql) to the
**main database only**, using `psql -v ON_ERROR_STOP=1 -d <main-database>
-f webapp/tools/pcs_rider_tables_main.sql` from the repository root. Confirm
the connection targets main and that both new tables are absent before running;
the script intentionally fails if either exists. Take the normal database
backup first. No Namespace database changes are needed. New installations
include the tables through the existing main model initialization. To undo
before storing data, manually drop `pcs_rider_results` then `pcs_riders` in main;
after data is stored, dropping these tables removes that data.

## Race-page integration

This integration parses supplied PCS race result HTML and matches riders against
a Namespace's CDB. Parsing HTML does not fetch a remote page.

## Usage

Parse HTML without persisting results:

```python
from webapp.integrations.pcs import parse_pcs_race_results_html

parsed = parse_pcs_race_results_html(html, source_url=source_url)
```

To persist results, use the configured Flask application context and a Namespace:

```python
from webapp.integrations.pcs import PcsClient

client = PcsClient(namespace)
summary = client.sync_race_results_from_html(html=html, source_url=source_url)
```

`PcsClient` replaces existing PCS results for the same Namespace and race name,
stores parsed rows in the main database, and maintains race result mappings.
It commits by default; pass `commit=False` to leave the transaction to the caller.
`PcsRiderMatcher` reads the Namespace CDB to match riders, with a configurable
`fuzzy_threshold` (default `0.84`). Exact matches use the namespace rider's
`pcs_slug` to build `https://www.procyclingstats.com/rider/{slug}/road` dynamically.
Result links with or without `/road` resolve to the same rider; deleted namespace
riders are excluded. Name matching remains the fallback when there is no exact
source-link match. The connection must address the selected isolated namespace.
Parsing requires BeautifulSoup, installed via
`beautifulsoup4` in the project requirements.

The legacy namespace `rider.pcs_rider_path` column is no longer used or created.
For existing namespaces, review and run
[`drop_rider_pcs_rider_path_namespace.sql`](../../../migrations/drop_rider_pcs_rider_path_namespace.sql)
manually against each namespace database, never main. Back up and export any
legacy path values first, compare them with `pcs_slug`, pause writes, apply the
script, then deploy/restart. It removes the obsolete field configuration entry
and column, preserving `pcs_slug` and Activity Monitor's separate path field.

## Activity Monitor profile parser

`activity_profile.py` is an offline parser for HTML supplied by the caller. It
extracts rider profile facts and PCS result rows, including non-finishes and
non-starts. It does not make network requests or write to the Activity Monitor
database. A missing or unrecognized result table remains an explicit parser
status and must not be interpreted as inactivity.

The Activity Monitor fetcher always uses the rider's `/road` profile and stores
that request path with the source check. PCS evidence counts as road activity
only when it is attached to a successful check whose saved path ends in
`/road`. Imported history keeps a validated PCS rider path when available;
local file paths are discarded. Older PCS results with no saved request path
remain visible as history but do not establish current road activity. Future-
dated rows are also retained only in pre-existing history and never count as
activity.

For Activity Monitor identity verification, only an exact match between the
WorldDB and PCS dates of birth is an automatic identity confirmation. A missing
date stays ambiguous; a conflicting date is a mismatch. Nationality and race
participation may be retained as context, but cannot confirm identity.

The pure `to_activity_monitor_evidence(...)` helper converts parsed rows to
`ActivityMonitorResult`-shaped dictionaries without saving them. It gives rows
a stable source key, retains the original parser row in `source_payload`, and
sets `identity_verified` only when the exact date-of-birth check passed. PCS
race identifiers that are not numeric remain in the source payload and map to
an empty database `race_id`; the PCS race URL is preserved separately.

Activity Monitor PCS evidence can be stored without a FirstCycling ID. The
target Namespace database must first receive the manual schema update described
in [`activity_monitor_allow_pcs_without_fc_id.md`](../activity_monitor_allow_pcs_without_fc_id.md).
New identity-based source keys remain stable within the Namespace and rider
identity generation; existing FC-based source keys are unchanged.

## Queued race parsing

The `pcs.get_race` worker fetches one bounded HTML response without redirects,
then calls `race.parse_pcs_race_html`. Landing pages, startlists, flat URLs such
as `uae-tour-2026-stage-6/results` and `uae-tour-2026-gc`, and slash-separated
edition URLs resolve to the base `uae-tour` slug and selected season. Explicit
seasons must match; returned canonical URLs must identify the same race and
requested result scope. Recognized PCS gender categories must also match.

The parser accepts the supplied HTML without network access:

```python
from webapp.integrations.pcs.race import parse_pcs_race_html, store_race

payload = parse_pcs_race_html(
    html, source_url=source_url, year=2026, gender="Men",
)
# In an application context and caller-owned transaction:
summary = store_race(params, payload, namespace_id=namespace.id)
```

`params` contains `race_slug`, `season_year`, `race_url`, and `gender` (use
`parse_race_url(source_url, year)` plus gender). The queue supplies its task
group's namespace ID and commits the race and result writes atomically.
No namespace CDB records are changed and no new schema changes are required.
The existing `pcs_races` table must be provisioned as described in
[pcs_races_main.md](../../../migrations/pcs_races_main.md).

Race-page results upsert `PcsRaceResult` within the supplied namespace, using the
canonical edition/result URL in its existing `race_name` field as the exact
scope key and rider name as the row identity. This keeps seasons, GC and stages
separate without changing the legacy table schema. These queued rows do not
perform namespace CDB rider matching; the legacy `PcsClient` matcher remains
available separately. Valid PCS rider links also upsert minimal `PcsRider`
records and shared `PcsRiderResult` rows without overwriting existing profile
facts. Repeat parses update existing rows. Stage dates and distances stay in the
source payload rather than overwriting overall race-edition facts. DNF and DNS
are retained in shared rider results; GC rows do not confirm participation.
Startlists and overviews without result rows only update the race. Missing or
unrecognized result tables on explicit result pages fail the task. No additional
pages or stages are fetched automatically.

For a single-call offline upsert, use
`PcsClient(namespace).sync_race_from_html(html=html, source_url=source_url)`.
It uses the namespace's `database_year` and `gender`; pass `year=2026` to
explicitly select the season. It commits by default and rolls back failed writes;
`commit=False` leaves transaction handling to the caller. The legacy
`sync_race_results_from_html` method retains its CDB matching behavior.
