# ProCyclingStats (PCS)

## Shared rider storage

`PcsRider` (`pcs_riders`) and `PcsRiderResult` (`pcs_rider_results`) use the
`main` bind and store PCS rider-page data independently of the Activity Monitor,
FirstCycling IDs, and Namespace CDB rider matches. The existing `PcsRaceResult`
race-page matching workflow is unchanged. No fetching, scheduling, or backfill
is added by these models.

The rider's canonical PCS slug is unique; `profile_url`, `display_name`,
`birth_date`, and `nationality` hold profile facts. `source_payload` retains
additional parser facts such as title, heading, and canonical URL. Only facts
currently extracted are modeled; height, weight, and team are not extracted by
the current rider-page parser.

Result columns follow `activity_profile.parse_pcs_html` rows: `year` maps to
`season`, `date` to `result_date`, and `result` to `result_position`. Other
fields retain their parser names. `race_id` is a PCS string grouping key, not
a numeric FirstCycling ID. `source_payload` retains the original row. Missing
season/date values are allowed, and headings, classifications, DNS, and DNF
remain distinguishable. `confirmed_participation` is the parser's row flag,
not confirmation that a PCS profile matches a CDB rider.

Future callers must supply a stable `source_result_key` per rider and result
row. The database enforces uniqueness of `(rider_id, source_result_key)` even
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
`fuzzy_threshold` (default `0.84`). Parsing requires BeautifulSoup, installed via
`beautifulsoup4` in the project requirements.

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
