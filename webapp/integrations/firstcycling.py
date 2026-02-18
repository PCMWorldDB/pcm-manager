"""FirstCycling API integration client"""
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app, has_app_context

from webapp.extensions import db
from webapp.models.main.firstcycling import (
    FirstCyclingRace,
    FirstCyclingRaceStage,
    FirstCyclingRider,
    FirstCyclingTeam,
)

RACE_TYPES_WHITELIST = ['ROAD']

class FirstCyclingAPIError(Exception):
    """Raised when the FirstCycling API request fails"""


class FirstCyclingClient:
    """Abstraction for the FirstCycling API and persistence in main database"""

    def __init__(self, db_session=None, base_url=None, api_path=None, default_year=2025, timeout_seconds=30):
        self.db_session = db_session or db.session
        self.default_year = default_year
        self.timeout_seconds = timeout_seconds

        if has_app_context():
            configured_base_url = current_app.config.get('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
        else:
            configured_base_url = os.getenv('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = os.getenv('FIRST_CYCLING_API_PATH')

        self.base_url = (base_url or configured_base_url).rstrip('/')
        self.api_path = (api_path or configured_api_path).strip('/')
        if not self.api_path:
            raise ValueError('FIRST_CYCLING_API_PATH must be set in config or environment variables')

    def _parse_date(self, date_value):
        if not date_value:
            return None
        if isinstance(date_value, str):
            try:
                return datetime.strptime(date_value, '%Y-%m-%d').date()
            except ValueError:
                return None
        return None

    def _parse_time(self, time_value):
        if not time_value:
            return None
        if isinstance(time_value, str):
            try:
                return datetime.strptime(time_value, '%H:%M').time()
            except ValueError:
                return None
        return None

    def _parse_float(self, float_value):
        if float_value is None:
            return None
        try:
            return float(float_value)
        except (TypeError, ValueError):
            return None

    def _redact_url(self, url):
        secret_segment = f'/{self.api_path}/'
        if secret_segment in url:
            return url.replace(secret_segment, '/<hidden>/')
        return url

    def _build_api_url(self, endpoint_path, params=None):
        endpoint_path = endpoint_path.lstrip('/')
        url = f'{self.base_url}/{self.api_path}/{endpoint_path}'
        if params:
            url = f'{url}?{urlencode(params)}'
        return url

    def _fetch_json(self, endpoint_path, params=None):
        request_url = self._build_api_url(endpoint_path, params=params)
        request = Request(
            request_url,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'pcm-manager-firstcycling-client/1.0',
            },
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode('utf-8')
            return json.loads(payload), request_url
        except HTTPError as exc:
            raise FirstCyclingAPIError(
                f'FirstCycling API HTTP {exc.code} for {self._redact_url(request_url)}'
            ) from exc
        except URLError as exc:
            raise FirstCyclingAPIError(
                f'FirstCycling API connection failure for {self._redact_url(request_url)}'
            ) from exc
        except json.JSONDecodeError as exc:
            raise FirstCyclingAPIError(
                f'FirstCycling API returned invalid JSON for {self._redact_url(request_url)}'
            ) from exc

    def _extract_entity_name(self, payload):
        if isinstance(payload, dict):
            for key in ('name', 'raceName', 'riderName', 'teamName'):
                value = payload.get(key)
                if value:
                    return str(value)
        return None

    def _extract_team_overview_rows(self, payload):
        if isinstance(payload, dict):
            teams = payload.get('teams')
            if isinstance(teams, list):
                return teams
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _extract_rider_overview_rows(self, payload):
        if isinstance(payload, dict):
            riders = payload.get('riders')
            if isinstance(riders, list):
                return riders
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _extract_race_calendar_rows(self, payload):
        if isinstance(payload, dict):
            races = payload.get('races')
            if isinstance(races, list):
                return races
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _race_record_from_payload(self, race_id, season_year, payload,synced_at=None):
        if not isinstance(payload, dict):
            payload = {}

        synced_at = synced_at or datetime.utcnow()
        race_type = payload.get('type')
        if isinstance(race_type, str) and race_type.strip().upper() not in RACE_TYPES_WHITELIST:
            return None

        stages = payload.get('stages')
        stage_count = len(stages) if isinstance(stages, list) else None

        return {
            'race_id': race_id,
            'season_year': season_year,
            'edition_id': payload.get('editionId'),
            'name': payload.get('raceName') or self._extract_entity_name(payload),
            'race_type': race_type,
            'category': payload.get('category'),
            'gender': payload.get('gender'),
            'nation_name': payload.get('nationName'),
            'description': payload.get('description'),
            'classification': payload.get('classification'),
            'classification_label': payload.get('classificationLabel'),
            'distance_km': self._parse_float(payload.get('distance')),
            'profile': payload.get('profile'),
            'start_date': self._parse_date(payload.get('startDate')),
            'end_date': self._parse_date(payload.get('endDate')),
            'start_time': self._parse_time(payload.get('startTime')),
            'end_time': self._parse_time(payload.get('endTime')),
            'start_location': payload.get('startLocation'),
            'end_location': payload.get('endLocation'),
            'stage_count': stage_count,
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _sync_race_stages(self, race, payload):
        stages = payload.get('stages') if isinstance(payload, dict) else None

        self.db_session.query(FirstCyclingRaceStage).filter_by(race_id=race.id).delete(synchronize_session=False)

        if not isinstance(stages, list):
            return

        stage_rows = []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            stage_rows.append(FirstCyclingRaceStage(
                race_id=race.id,
                stage_id=stage.get('stageId'),
                stage_number=stage.get('stageNumber'),
                stage_name=stage.get('stageName'),
                stage_date=self._parse_date(stage.get('stageDate')),
                stage_distance_km=self._parse_float(stage.get('stageDistance')),
                stage_profile=stage.get('stageProfile'),
                stage_start_location=stage.get('stageStartLocation'),
                stage_end_location=stage.get('stageEndLocation'),
                stage_start_time=self._parse_time(stage.get('stageStartTime')),
                stage_end_time=self._parse_time(stage.get('stageEndTime')),
            ))

        if stage_rows:
            self.db_session.add_all(stage_rows)

    def _team_record_from_payload(self, team_id, payload, source_endpoint, synced_at=None):
        description = payload.get('description', payload) if isinstance(payload, dict) else {}
        rankings = payload.get('rankings', {}) if isinstance(payload, dict) else {}
        synced_at = synced_at or datetime.utcnow()

        team_name = description.get('teamName') or self._extract_entity_name(payload)

        return {
            'team_id': team_id,
            'team_year': description.get('teamYear'),
            'team_name': team_name,
            'nation_name': description.get('nationName'),
            'division': description.get('division'),
            'gender': description.get('gender'),
            'uci_code': description.get('uciCode'),
            'ranking_title': rankings.get('title') if isinstance(rankings, dict) else None,
            'ranking_rank': rankings.get('rank') if isinstance(rankings, dict) else None,
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _rider_record_from_payload(self, rider_id, payload, source_endpoint, synced_at=None, season_year=None):
        description = payload.get('description', payload) if isinstance(payload, dict) else {}
        team_data = payload.get('team', {}) if isinstance(payload, dict) else {}
        synced_at = synced_at or datetime.utcnow()

        first_name = description.get('firstName')
        last_name = description.get('lastName')
        full_name = ' '.join(part for part in [first_name, last_name] if part).strip()

        return {
            'rider_id': rider_id,
            'season_year': season_year,
            'first_name': first_name,
            'last_name': last_name,
            'gender': description.get('gender'),
            'height': description.get('height'),
            'birth_date': self._parse_date(description.get('birthDate')),
            'nation_name': description.get('nationName'),
            'team_id': team_data.get('teamId') if isinstance(team_data, dict) else None,
            'team_name': team_data.get('teamName') if isinstance(team_data, dict) else None,
            'name': full_name or self._extract_entity_name(payload),
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _upsert_race(self, race_id, season_year, payload, source_endpoint):
        normalized = self._race_record_from_payload(
            race_id=race_id,
            season_year=season_year,
            payload=payload
        )
        if normalized is None:
            return None

        race = self.db_session.query(FirstCyclingRace).filter_by(
            race_id=race_id,
            season_year=season_year,
        ).first()
        if race is None:
            race = FirstCyclingRace(race_id=race_id, season_year=season_year)
            self.db_session.add(race)

        race.edition_id = normalized['edition_id']
        race.name = normalized['name']
        race.race_type = normalized['race_type']
        race.category = normalized['category']
        race.gender = normalized['gender']
        race.nation_name = normalized['nation_name']
        race.description = normalized['description']
        race.classification = normalized['classification']
        race.classification_label = normalized['classification_label']
        race.distance_km = normalized['distance_km']
        race.profile = normalized['profile']
        race.start_date = normalized['start_date']
        race.end_date = normalized['end_date']
        race.start_time = normalized['start_time']
        race.end_time = normalized['end_time']
        race.start_location = normalized['start_location']
        race.end_location = normalized['end_location']
        race.stage_count = normalized['stage_count']
        race.last_synced_at = normalized['last_synced_at']

        self.db_session.flush()
        self._sync_race_stages(race=race, payload=payload)

        return race

    def _upsert_team(self, team_id, payload, source_endpoint):
        team = self.db_session.query(FirstCyclingTeam).filter_by(team_id=team_id).first()
        if team is None:
            team = FirstCyclingTeam(team_id=team_id)
            self.db_session.add(team)

        normalized = self._team_record_from_payload(team_id=team_id, payload=payload, source_endpoint=source_endpoint)

        team.team_year = normalized['team_year']
        team.team_name = normalized['team_name']
        team.nation_name = normalized['nation_name']
        team.division = normalized['division']
        team.gender = normalized['gender']
        team.uci_code = normalized['uci_code']
        team.ranking_title = normalized['ranking_title']
        team.ranking_rank = normalized['ranking_rank']
        team.name = normalized['name']
        team.source_endpoint = normalized['source_endpoint']
        team.last_synced_at = normalized['last_synced_at']
        return team

    def _upsert_rider(self, rider_id, payload, source_endpoint, season_year=None):
        rider = self.db_session.query(FirstCyclingRider).filter_by(rider_id=rider_id).first()
        if rider is None:
            rider = FirstCyclingRider(rider_id=rider_id)
            self.db_session.add(rider)

        normalized = self._rider_record_from_payload(
            rider_id=rider_id,
            payload=payload,
            source_endpoint=source_endpoint,
            season_year=season_year,
        )

        rider.season_year = normalized['season_year']
        rider.first_name = normalized['first_name']
        rider.last_name = normalized['last_name']
        rider.gender = normalized['gender']
        rider.height = normalized['height']
        rider.birth_date = normalized['birth_date']
        rider.nation_name = normalized['nation_name']
        rider.team_id = normalized['team_id']
        rider.team_name = normalized['team_name']
        rider.name = normalized['name']
        rider.source_endpoint = normalized['source_endpoint']
        rider.last_synced_at = normalized['last_synced_at']
        return rider

    def get_race(self, race_id, season_year=None):
        season_year = season_year or self.default_year
        endpoint = f'{season_year}/{race_id}.json'
        return self._fetch_json(endpoint)

    def get_race_calendar(self, season_year=None):
        season_year = season_year or self.default_year
        endpoint = f'{season_year}/raceCalendar.json'
        return self._fetch_json(endpoint)

    def get_team_overview(self, season_year=None):
        season_year = season_year or self.default_year
        endpoint = f'{season_year}/teamOverview.json'
        return self._fetch_json(endpoint)

    def get_rider_overview(self, season_year=None):
        season_year = season_year or self.default_year
        endpoint = f'{season_year}/riderOverview.json'
        return self._fetch_json(endpoint)

    def get_rider(self, rider_id):
        return self._fetch_json('riderData.php', params={'id': rider_id})

    def get_team(self, team_id):
        return self._fetch_json('teamData.php', params={'id': team_id})

    def get_latest_results(self):
        return self._fetch_json('latestResults.php')

    def get_latest_results_today(self):
        return self._fetch_json('latestResultsTODAY.php')

    def sync_race_calendar(self, year, gender, commit=True):
        payload, source_endpoint = self.get_race_calendar(season_year=year)

        race_rows = self._extract_race_calendar_rows(payload)
        synced = 0
        skipped = 0
        filtered_out = 0

        for row in race_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue

            if row.get('gender') != gender:
                filtered_out += 1
                continue

            race_id = row.get('raceId') or row.get('id')
            if race_id is None:
                skipped += 1
                continue

            try:
                race_id = int(race_id)
            except (TypeError, ValueError):
                skipped += 1
                continue

            race = self._upsert_race(
                race_id=race_id,
                season_year=year,
                payload=row,
                source_endpoint=source_endpoint,
            )

            if race is None:
                skipped += 1
            else:
                synced += 1

        if commit:
            self.db_session.commit()

        return {
            'requested': len(race_rows),
            'synced': synced,
            'skipped': skipped,
            'filtered_out': filtered_out,
        }

    def sync_teams(self, year, gender, limit=None, commit=True):
        payload, source_endpoint = self.get_team_overview(season_year=year)

        team_rows = self._extract_team_overview_rows(payload)
        team_ids = []
        filtered_out = 0
        skipped = 0

        for row in team_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue

            if row.get('gender') != gender:
                filtered_out += 1
                continue

            team_id = row.get('teamId') or row.get('id')
            if team_id is None:
                skipped += 1
                continue

            try:
                team_ids.append(int(team_id))
            except (TypeError, ValueError):
                skipped += 1

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            team_ids = team_ids[:limit]

        synced = 0
        failures = []

        for team_id in team_ids:
            try:
                team_payload, team_source_endpoint = self.get_team(team_id)
                self._upsert_team(team_id=team_id, payload=team_payload, source_endpoint=team_source_endpoint)
                synced += 1
            except Exception as exc:
                failures.append((team_id, str(exc)))

        if commit:
            self.db_session.commit()

        return {
            'requested': len(team_ids),
            'fetched': synced,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'failures': failures,
        }

    def sync_riders(self, year, gender, limit=None, commit=True):
        payload, source_endpoint = self.get_rider_overview(season_year=year)

        rider_rows = self._extract_rider_overview_rows(payload)
        rider_ids = []
        filtered_out = 0
        skipped = 0

        for row in rider_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue

            # if row.get('gender') != gender:
            #     filtered_out += 1
            #     continue

            rider_id = row.get('id') or row.get('riderId')
            if rider_id is None:
                skipped += 1
                continue

            try:
                rider_ids.append(int(rider_id))
            except (TypeError, ValueError):
                skipped += 1

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            rider_ids = rider_ids[:limit]

        synced = 0
        failures = []

        for rider_id in rider_ids:
            try:
                rider_payload, rider_source_endpoint = self.get_rider(rider_id)
                self._upsert_rider(
                    rider_id=rider_id,
                    payload=rider_payload,
                    source_endpoint=rider_source_endpoint,
                    season_year=year,
                )
                synced += 1
            except Exception as exc:
                failures.append((rider_id, str(exc)))

        if commit:
            self.db_session.commit()

        return {
            'requested': len(rider_ids),
            'fetched': synced,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'failures': failures,
        }

    def sync_latest_results(self, only_today=False, commit=True):
        if only_today:
            payload, source_endpoint = self.get_latest_results_today()
            feed_name = 'latestResultsTODAY'
        else:
            payload, source_endpoint = self.get_latest_results()
            feed_name = 'latestResults'

        snapshot = self.save_feed_snapshot(
            feed_name=feed_name,
            payload=payload,
            source_endpoint=source_endpoint,
            season_year=self.default_year,
            commit=False,
        )
        if commit:
            self.db_session.commit()
        return snapshot

    def sync_season_roster(self, season_year, gender, commit=True):
        """Sync race calendar, teams, and riders for a season.

        Steps:
        1. Fetch and snapshot race calendar, team overview, and rider overview feeds.
        2. Upsert race rows from race calendar (excluding CYCLOCROSS races).
        3. Fetch each team detail payload and bulk upsert into database.
        4. Fetch each rider detail payload and bulk upsert into database.
        """

        race_summary = self.sync_race_calendar(year=season_year, gender=gender, commit=False)
        team_summary = self.sync_teams(year=season_year, gender=gender, commit=False, limit=2)
        rider_summary = self.sync_riders(year=season_year, gender=gender, commit=False, limit=2)

        if commit:
            self.db_session.commit()

        return {
            'season_year': season_year,
            'races': {
                'requested': race_summary['requested'],
                'synced': race_summary['synced'],
                'skipped': race_summary['skipped'],
                'filtered_out': race_summary['filtered_out'],
            },
            'teams': {
                'requested': team_summary['requested'],
                'fetched': team_summary['fetched'],
                'failed': team_summary['failed'],
                'filtered_out': team_summary['filtered_out'],
                'skipped': team_summary['skipped'],
            },
            'riders': {
                'requested': rider_summary['requested'],
                'fetched': rider_summary['fetched'],
                'failed': rider_summary['failed'],
                'filtered_out': rider_summary['filtered_out'],
                'skipped': rider_summary['skipped'],
            },
            'failures': {
                'teams': team_summary['failures'],
                'riders': rider_summary['failures'],
            },
        }
