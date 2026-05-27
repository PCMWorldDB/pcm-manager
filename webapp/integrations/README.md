# Integrations

This folder contains external integration clients used by the application.

## Supported Integrations

### FirstCycling (`firstcycling.py`)

`FirstCyclingClient` is an abstraction over the FirstCycling API test endpoint.

#### Endpoint coverage

- Race calendar (2025): `/{FIRST_CYCLING_API_PATH}/2025/raceCalendar.json`
- Team overview (2025): `/{FIRST_CYCLING_API_PATH}/2025/teamOverview.json`
- Race details by race ID: `/{FIRST_CYCLING_API_PATH}/2025/{race-id}.json`
- Rider details by rider ID: `/{FIRST_CYCLING_API_PATH}/rider/{rider-id}.json`
- Team details by team ID: `/{FIRST_CYCLING_API_PATH}/team/{team-id}.json`

- THESE ENDPOINTS NOT WORKING
- Latest results: `/{FIRST_CYCLING_API_PATH}/latestResults.php`
- Latest results today: `/{FIRST_CYCLING_API_PATH}/latestResultsTODAY.php`


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
- `sync_riders(season_year=2025, gender='Men')` (loads rider IDs from `riderOverview` and fetches rider details)
- `sync_race_results_window(season_year=2025, gender='Men', start_date='2025-03-01', end_date='2025-03-31')`
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

### Discord (`discord.py`)

`DiscordClient` is a scaffold for future Discord integration work.

#### Intended configuration

- `DISCORD_WEBHOOK_URL`
- `DISCORD_BOT_TOKEN`

#### Current status

- Provider tab is available in the admin Integrations page.
- Runtime behavior is intentionally not implemented yet.

## Adding new integrations

- Create a new integration module in this folder.
- Keep network logic and persistence logic encapsulated in a client class.
- Keep sensitive credentials/URL segments in environment variables.
- Ensure endpoint failures raise clear exceptions without exposing secrets.
