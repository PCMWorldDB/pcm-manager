# Integrations

This folder contains external integration clients used by the application.

## Supported Integrations

### FirstCycling (`firstcycling.py`)

`FirstCyclingClient` is an abstraction over the FirstCycling API test endpoint.

#### Endpoint coverage

- Race calendar (2025): `/{FIRST_CYCLING_API_PATH}/2025/raceCalendar.json`
- Team overview (2025): `/{FIRST_CYCLING_API_PATH}/2025/teamOverview.json`
- Rider overview (2025): `/{FIRST_CYCLING_API_PATH}/2025/riderOverview.json`
- Race details by race ID: `/{FIRST_CYCLING_API_PATH}/2025/{race-id}.json`
- Rider details by rider ID: `/{FIRST_CYCLING_API_PATH}/riderData.php?id={rider-id}`
- Team details by team ID: `/{FIRST_CYCLING_API_PATH}/teamData.php?id={team-id}`

- THESE ENDPOINTS NOT WORKING
- Latest results: `/{FIRST_CYCLING_API_PATH}/latestResults.php`
- Latest results today: `/{FIRST_CYCLING_API_PATH}/latestResultsTODAY.php`

The integration defaults to **2025** for year-based endpoints.

#### Configuration

Set these environment variables:

- `FIRST_CYCLING_BASE_URL` (default: `https://firstcycling.com`)
- `FIRST_CYCLING_API_PATH` (default: `api-token`)

`FIRST_CYCLING_API_PATH` is treated as sensitive. The client redacts this path segment in raised error messages so logs do not leak the real value.

#### Persistence behavior

The client can fetch data and store it in main database tables:

- `firstcycling_races`
- `firstcycling_teams`
- `firstcycling_riders`

Use sync methods to fetch and persist in one step:

- `sync_race(race_id, season_year=2025)`
- `sync_race_calendar(season_year=2025, gender='Men')`
- `sync_teams(season_year=2025, gender='Men')`
- `sync_riders(season_year=2025, gender='Men')`
- `sync_latest_results(only_today=False)`

#### Minimal usage example

```python
from webapp.integrations.firstcycling import FirstCyclingClient

client = FirstCyclingClient()

# Fetch + persist race calendar snapshot and race rows
client.sync_race_calendar(season_year=2025)

# Fetch + persist one race by ID (e.g. TDU at race-id 1)
client.sync_race(1, season_year=2025)
```

## Adding new integrations

- Create a new integration module in this folder.
- Keep network logic and persistence logic encapsulated in a client class.
- Keep sensitive credentials/URL segments in environment variables.
- Ensure endpoint failures raise clear exceptions without exposing secrets.
