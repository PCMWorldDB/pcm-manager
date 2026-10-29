# FirstCycling

`FirstCyclingClient` fetches FirstCycling API data and synchronizes races, stages,
results, teams, and riders into the main database. Background sync jobs in
`webapp/tasks.py` use this client.

## Configuration

- `FIRST_CYCLING_BASE_URL`: defaults to `https://firstcycling.com`.
- `FIRST_CYCLING_API_PATH`: required API path; treated as sensitive and redacted
  from request errors.
- Sync settings in `webapp/config.py` control retries, request delays, logging,
  sync method, and whether to fetch only missing data.

## Usage

Run persistence methods inside the configured Flask application context:

```python
from webapp.integrations.firstcycling import FirstCyclingClient

client = FirstCyclingClient()
client.sync_race_calendar(season_year=2026, gender='Men')
client.sync_race(1, season_year=2026)
```

Other sync methods include `sync_teams`, `sync_riders`, and
`sync_race_results_window`. They fetch external data and persist it in main
database tables; use the admin Integrations workflow for normal background syncs.

API paths include `/{api-path}/{year}/raceCalendar.json`,
`/{api-path}/{year}/teamOverview.json`, `/{api-path}/{year}/{race-id}.json`,
`/{api-path}/rider/{rider-id}.json`, and `/{api-path}/team/{team-id}.json`.
The previous integration notes report the `latestResults.php` and
`latestResultsTODAY.php` endpoints as not working.
