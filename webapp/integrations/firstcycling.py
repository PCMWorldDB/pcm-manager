"""FirstCycling API integration client"""
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app, has_app_context

from webapp.extensions import db
from webapp.models.main.base import ExternalSyncAPICallLog
from webapp.models.main.firstcycling import (
    FirstCyclingRace,
    FirstCyclingRaceResult,
    FirstCyclingRaceStage,
    FirstCyclingRider,
    FirstCyclingTeam,
)

RACE_TYPES_WHITELIST = ['ROAD']
logger = logging.getLogger('firstcycling')


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _normalize_gender_token(value):
    token = str(value or '').strip().lower()
    if token in ('m', 'men', 'male', 'man'):
        return 'men'
    if token in ('w', 'f', 'women', 'woman', 'female', 'ladies'):
        return 'women'
    return token


def _gender_matches(row_gender, selected_gender):
    """Return True when row gender aligns with selected gender.

    Missing row gender is treated as compatible so we do not drop valid rows
    from providers that omit gender in overview payloads.
    """
    normalized_selected = _normalize_gender_token(selected_gender)
    normalized_row = _normalize_gender_token(row_gender)

    if not normalized_selected:
        return True
    if not normalized_row:
        return True
    return normalized_row == normalized_selected

class FirstCyclingAPIError(Exception):
    """Raised when the FirstCycling API request fails"""


class FirstCyclingClient:
    """Abstraction for the FirstCycling API and persistence in main database"""

    def __init__(
        self,
        db_session=None,
        base_url=None,
        api_path=None,
        timeout_seconds=30,
        external_sync_log_id=None,
    ):
        self.db_session = db_session or db.session
        self.timeout_seconds = timeout_seconds
        self.external_sync_log_id = external_sync_log_id

        if has_app_context():
            configured_base_url = current_app.config.get('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
            configured_verbose_sync = current_app.config.get('FIRST_CYCLING_SYNC_VERBOSE', False)
            configured_log_interval = current_app.config.get('FIRST_CYCLING_SYNC_LOG_INTERVAL', 50)
            configured_sync_method = current_app.config.get('FIRST_CYCLING_SYNC_METHOD', 'bulk_refresh')
            configured_fetch_only_missing = current_app.config.get('FIRST_CYCLING_FETCH_ONLY_MISSING', False)
            configured_request_delay = current_app.config.get('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 0.35)
            configured_retry_attempts = current_app.config.get('FIRST_CYCLING_RETRY_ATTEMPTS', 4)
            configured_retry_backoff = current_app.config.get('FIRST_CYCLING_RETRY_BACKOFF_SECONDS', 1.0)
        else:
            configured_base_url = os.getenv('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = os.getenv('FIRST_CYCLING_API_PATH')
            configured_verbose_sync = os.getenv('FIRST_CYCLING_SYNC_VERBOSE')
            configured_log_interval = os.getenv('FIRST_CYCLING_SYNC_LOG_INTERVAL', 50)
            configured_sync_method = os.getenv('FIRST_CYCLING_SYNC_METHOD', 'bulk_refresh')
            configured_fetch_only_missing = os.getenv('FIRST_CYCLING_FETCH_ONLY_MISSING')
            configured_request_delay = os.getenv('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 0.35)
            configured_retry_attempts = os.getenv('FIRST_CYCLING_RETRY_ATTEMPTS', 4)
            configured_retry_backoff = os.getenv('FIRST_CYCLING_RETRY_BACKOFF_SECONDS', 1.0)

        self.base_url = (base_url or configured_base_url).rstrip('/')
        configured_api_path = api_path if api_path is not None else configured_api_path
        self.api_path = str(configured_api_path or '').strip('/')
        if not self.api_path:
            raise ValueError('FIRST_CYCLING_API_PATH must be set in config or environment variables')

        self.verbose_sync = _as_bool(configured_verbose_sync, default=False)
        sync_method_value = str(configured_sync_method or 'bulk_refresh').strip().lower()
        if sync_method_value not in ('bulk_refresh', 'incremental_upsert'):
            logger.warning(
                'Unsupported FIRST_CYCLING_SYNC_METHOD=%s; falling back to bulk_refresh',
                sync_method_value,
            )
            sync_method_value = 'bulk_refresh'
        self.sync_method = sync_method_value
        self.fetch_only_missing = _as_bool(configured_fetch_only_missing, default=False)

        try:
            self.sync_log_interval = max(int(configured_log_interval), 1)
        except (TypeError, ValueError):
            self.sync_log_interval = 50

        try:
            self.request_delay_seconds = max(float(configured_request_delay), 0.0)
        except (TypeError, ValueError):
            self.request_delay_seconds = 0.35

        try:
            self.retry_attempts = max(int(configured_retry_attempts), 0)
        except (TypeError, ValueError):
            self.retry_attempts = 4

        try:
            self.retry_backoff_seconds = max(float(configured_retry_backoff), 0.1)
        except (TypeError, ValueError):
            self.retry_backoff_seconds = 1.0

        self._last_request_timestamp = 0.0

    def _throttle_request(self):
        """Apply a minimum delay between outbound FirstCycling requests."""
        if self.request_delay_seconds <= 0:
            return

        now = time.monotonic()
        elapsed = now - self._last_request_timestamp
        if elapsed < self.request_delay_seconds:
            time.sleep(self.request_delay_seconds - elapsed)

        self._last_request_timestamp = time.monotonic()

    def _parse_retry_after(self, exc):
        headers = getattr(exc, 'headers', None)
        if headers is None:
            return None

        retry_after_value = headers.get('Retry-After')
        if retry_after_value is None:
            return None

        try:
            retry_after = float(retry_after_value)
        except (TypeError, ValueError):
            return None

        return max(retry_after, 0.0)

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

    def _record_api_call(
        self,
        *,
        endpoint_path,
        params,
        request_url,
        attempt,
        max_attempts,
        outcome,
        duration_ms,
        http_status=None,
        response_bytes=None,
        error=None,
    ):
        try:
            params_json = json.dumps(params, default=str, sort_keys=True) if params else None
            error_message = str(error) if error is not None else None
            values = {
                'external_sync_log_id': self.external_sync_log_id,
                'provider': 'firstcycling',
                'endpoint': endpoint_path.lstrip('/'),
                'request_url': self._redact_url(request_url),
                'request_params_json': params_json,
                'attempt': attempt,
                'max_attempts': max_attempts,
                'outcome': outcome,
                'http_status': http_status,
                'duration_ms': duration_ms,
                'response_bytes': response_bytes,
                'error_type': type(error).__name__ if error is not None else None,
                'error_message': error_message[:4000] if error_message else None,
            }
            with db.engines['main'].begin() as conn:
                conn.execute(ExternalSyncAPICallLog.__table__.insert().values(**values))
        except Exception:
            logger.exception('Failed to record FirstCycling API call log for endpoint %s', endpoint_path)

    def _fetch_json(self, endpoint_path, params=None):
        request_url = self._build_api_url(endpoint_path, params=params)
        total_attempts = self.retry_attempts + 1

        for attempt in range(1, total_attempts + 1):
            request = Request(
                request_url,
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'worlddb-firstcycling-client/1.0',
                },
            )

            self._throttle_request()
            request_started = time.perf_counter()

            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    response_status = getattr(response, 'status', None) or response.getcode()
                    response_body = response.read()
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                payload = response_body.decode('utf-8')
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as exc:
                    self._record_api_call(
                        endpoint_path=endpoint_path,
                        params=params,
                        request_url=request_url,
                        attempt=attempt,
                        max_attempts=total_attempts,
                        outcome='invalid_json',
                        http_status=response_status,
                        duration_ms=duration_ms,
                        response_bytes=len(response_body),
                        error=exc,
                    )
                    logger.error('❌ FirstCycling invalid JSON response: %s', self._redact_url(request_url))
                    raise FirstCyclingAPIError(
                        f'FirstCycling API returned invalid JSON for {self._redact_url(request_url)}'
                    ) from exc

                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='success',
                    http_status=response_status,
                    duration_ms=duration_ms,
                    response_bytes=len(response_body),
                )
                logger.info('✅ FirstCycling response HTTP %s: %s', response_status, self._redact_url(request_url))
                return parsed, request_url
            except HTTPError as exc:
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                is_retryable = exc.code == 429 or 500 <= exc.code < 600
                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='http_retry' if is_retryable and attempt < total_attempts else 'http_error',
                    http_status=exc.code,
                    duration_ms=duration_ms,
                    error=exc,
                )
                if is_retryable and attempt < total_attempts:
                    retry_after = self._parse_retry_after(exc)
                    wait_seconds = retry_after if retry_after is not None else self.retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        '❌ FirstCycling response HTTP %s, retrying with backoff in %.2fs (attempt %s/%s): %s',
                        exc.code,
                        wait_seconds,
                        attempt,
                        total_attempts,
                        self._redact_url(request_url),
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.error(
                    '❌ FirstCycling response HTTP %s (attempt %s/%s): %s',
                    exc.code,
                    attempt,
                    total_attempts,
                    self._redact_url(request_url),
                )

                raise FirstCyclingAPIError(
                    f'FirstCycling API HTTP {exc.code} for {self._redact_url(request_url)}'
                ) from exc
            except URLError as exc:
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='connection_retry' if attempt < total_attempts else 'connection_error',
                    duration_ms=duration_ms,
                    error=exc,
                )
                if attempt < total_attempts:
                    wait_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        '❌ FirstCycling connection failure, retrying in %.2fs (attempt %s/%s): %s',
                        wait_seconds,
                        attempt,
                        total_attempts,
                        self._redact_url(request_url),
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.error(
                    '❌ FirstCycling connection failure (attempt %s/%s): %s',
                    attempt,
                    total_attempts,
                    self._redact_url(request_url),
                )

                raise FirstCyclingAPIError(
                    f'FirstCycling API connection failure for {self._redact_url(request_url)}'
                ) from exc

        raise FirstCyclingAPIError(
            f'FirstCycling API request failed after retries for {self._redact_url(request_url)}'
        )

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

    def _extract_riders_result_rows(self, payload):
        if not isinstance(payload, dict):
            return {}

        riders = payload.get('riders')
        if isinstance(riders, dict):
            return riders

        info = payload.get('info')
        if isinstance(info, dict):
            info_riders = info.get('riders')
            if isinstance(info_riders, dict):
                return info_riders

        return {}

    def _parse_int(self, value):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_input_date(self, value, field_name):
        if value is None:
            raise ValueError(f'{field_name} is required')
        if isinstance(value, datetime):
            return value.date()
        if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day') and not isinstance(value, str):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError(f'{field_name} is required')
            try:
                return datetime.strptime(raw, '%Y-%m-%d').date()
            except ValueError as exc:
                raise ValueError(f'{field_name} must be YYYY-MM-DD') from exc
        raise ValueError(f'{field_name} must be a date or YYYY-MM-DD string')

    def _date_windows_overlap(self, range_start, range_end, window_start, window_end):
        return range_start <= window_end and range_end >= window_start

    def _parse_is_still_in_race(self, value):
        if isinstance(value, bool):
            return value

        marker = str(value or '').strip().upper()
        if marker in ('Y', 'YES', 'TRUE', '1'):
            return True
        if marker in ('N', 'NO', 'FALSE', '0'):
            return False

        return None

    def _stage_number_from_key(self, stage_key):
        if not isinstance(stage_key, str):
            return None

        match = re.match(r'^stage-(\d+)$', stage_key.strip(), flags=re.IGNORECASE)
        if not match:
            return None

        return self._parse_int(match.group(1))

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
            'name': team_name,
            'nation_name': description.get('nationName'),
            'division': description.get('division'),
            'gender': description.get('gender'),
            'uci_code': description.get('uciCode'),
            'ranking_title': rankings.get('title') if isinstance(rankings, dict) else None,
            'ranking_rank': rankings.get('rank') if isinstance(rankings, dict) else None,
            'rider_ids': self._extract_rider_ids_from_team_payload(payload),
            'source_endpoint': source_endpoint,
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _extract_rider_ids_from_team_payload(self, payload):
        if not isinstance(payload, dict):
            return []

        raw_riders = payload.get('riders')
        if not isinstance(raw_riders, list):
            return []

        rider_ids = []
        seen = set()
        for rider_id in raw_riders:
            parsed = self._parse_int(rider_id)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            rider_ids.append(parsed)

        return rider_ids

    def _rider_record_from_payload(self, rider_id, payload, source_endpoint, synced_at=None, season_year=None):
        description = payload.get('description', payload) if isinstance(payload, dict) else {}
        team_data = payload.get('team', {}) if isinstance(payload, dict) else {}
        synced_at = synced_at or datetime.utcnow()

        first_name = description.get('firstName')
        last_name = description.get('lastName')
        full_name = ' '.join(part for part in [first_name, last_name] if part).strip()

        raw_team_id = team_data.get('teamId') if isinstance(team_data, dict) else None
        if raw_team_id is None and isinstance(description, dict):
            raw_team_id = description.get('teamId')
        team_id = self._parse_int(raw_team_id)

        team_name = None
        if isinstance(team_data, dict):
            team_name = team_data.get('teamName') or team_data.get('name')
        elif isinstance(team_data, str):
            team_name = team_data

        if not team_name and isinstance(description, dict):
            team_name = description.get('teamName')

        return {
            'rider_id': rider_id,
            'season_year': season_year,
            'first_name': first_name,
            'last_name': last_name,
            'gender': description.get('gender'),
            'height': description.get('height'),
            'birth_date': self._parse_date(description.get('birthDate')),
            'nation_name': description.get('nationName'),
            'team_id': team_id,
            'team_name': team_name,
            'name': full_name or self._extract_entity_name(payload),
            'source_endpoint': source_endpoint,
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
        normalized = self._team_record_from_payload(team_id=team_id, payload=payload, source_endpoint=source_endpoint)
        return self._upsert_team_record(team_id=team_id, normalized=normalized)

    def _upsert_team_record(self, team_id, normalized):
        team = self.db_session.query(FirstCyclingTeam).filter_by(team_id=team_id).first()
        is_new = team is None
        if team is None:
            team = FirstCyclingTeam(team_id=team_id)
            self.db_session.add(team)

        team.team_year = normalized['team_year']
        team.team_name = normalized['team_name']
        team.nation_name = normalized['nation_name']
        team.division = normalized['division']
        team.gender = normalized['gender']
        team.uci_code = normalized['uci_code']
        team.ranking_title = normalized['ranking_title']
        team.ranking_rank = normalized['ranking_rank']
        team.rider_ids = normalized['rider_ids']
        team.name = normalized['name']
        team.source_endpoint = normalized['source_endpoint']
        team.last_synced_at = normalized['last_synced_at']
        if self.verbose_sync:
            logger.info(
                'Team upsert %s: team_id=%s name=%s',
                'created' if is_new else 'updated',
                team_id,
                team.team_name,
            )
        return team

    def _to_model_kwargs(self, model_cls, payload):
        """Return payload keys that map to actual SQLAlchemy model columns."""
        allowed = {column.name for column in model_cls.__table__.columns}
        return {key: value for key, value in payload.items() if key in allowed}

    def _upsert_rider(self, rider_id, payload, source_endpoint, season_year=None):
        normalized = self._rider_record_from_payload(
            rider_id=rider_id,
            payload=payload,
            source_endpoint=source_endpoint,
            season_year=season_year,
        )
        return self._upsert_rider_record(rider_id=rider_id, normalized=normalized)

    def _upsert_rider_record(self, rider_id, normalized):
        rider = self.db_session.query(FirstCyclingRider).filter_by(rider_id=rider_id).first()
        is_new = rider is None
        if rider is None:
            rider = FirstCyclingRider(rider_id=rider_id)
            self.db_session.add(rider)

        rider.first_name = normalized['first_name']
        rider.last_name = normalized['last_name']
        rider.gender = normalized['gender']
        rider.height = normalized['height']
        rider.birth_date = normalized['birth_date']
        rider.nation_name = normalized['nation_name']
        rider.team_id = normalized['team_id']
        rider.team_name = normalized['team_name']
        rider.name = normalized['name']
        rider.last_synced_at = normalized['last_synced_at']
        if self.verbose_sync:
            logger.info(
                'Rider upsert %s: rider_id=%s name=%s team_id=%s',
                'created' if is_new else 'updated',
                rider_id,
                rider.name,
                rider.team_id,
            )
        return rider

    def _filter_missing_team_ids(self, team_ids):
        if not team_ids:
            return [], 0

        existing_ids = {
            row[0]
            for row in self.db_session.query(FirstCyclingTeam.team_id)
            .filter(FirstCyclingTeam.team_id.in_(team_ids))
            .all()
            if row and row[0] is not None
        }
        missing_ids = [team_id for team_id in team_ids if team_id not in existing_ids]
        return missing_ids, len(existing_ids)

    def _filter_missing_rider_ids(self, rider_ids):
        if not rider_ids:
            return [], 0

        existing_ids = {
            row[0]
            for row in self.db_session.query(FirstCyclingRider.rider_id)
            .filter(FirstCyclingRider.rider_id.in_(rider_ids))
            .all()
            if row and row[0] is not None
        }
        missing_ids = [rider_id for rider_id in rider_ids if rider_id not in existing_ids]
        return missing_ids, len(existing_ids)

    def _collect_rider_ids_from_overview(self, year, gender):
        """Collect unique rider IDs from the rider overview feed for one season."""
        payload, _source_endpoint = self.get_rider_overview(season_year=year)
        rider_rows = self._extract_rider_overview_rows(payload)
        rider_ids = []
        seen = set()
        skipped_rows = 0
        filtered_out = 0

        for row in rider_rows:
            if isinstance(row, dict):
                if not _gender_matches(row.get('gender'), gender):
                    filtered_out += 1
                    continue
                raw_rider_id = row.get('riderId') or row.get('id')
            else:
                raw_rider_id = row

            parsed_rider_id = self._parse_int(raw_rider_id)
            if parsed_rider_id is None:
                skipped_rows += 1
                continue
            if parsed_rider_id in seen:
                continue
            seen.add(parsed_rider_id)
            rider_ids.append(parsed_rider_id)

        return sorted(rider_ids), skipped_rows, filtered_out

    def get_race(self, race_id, season_year):
        endpoint = f'{season_year}/{race_id}.json'
        return self._fetch_json(endpoint)

    def get_race_calendar(self, season_year):
        endpoint = f'{season_year}/raceCalendar.json'
        return self._fetch_json(endpoint)

    def get_team_overview(self, season_year, gender):
        endpoint = f'{season_year}/teamOverview.json'
        payload, source_endpoint = self._fetch_json(endpoint)

        team_rows = self._extract_team_overview_rows(payload)
        filtered_rows = [
            row
            for row in team_rows
            if isinstance(row, dict)
            and _gender_matches(row.get('gender'), gender)
        ]

        if isinstance(payload, dict):
            filtered_payload = dict(payload)
            filtered_payload['teams'] = filtered_rows
            return filtered_payload, source_endpoint

        return filtered_rows, source_endpoint

    def get_rider_overview(self, season_year):
        endpoint = f'{season_year}/riderOverview.json'
        return self._fetch_json(endpoint)

    def get_rider(self, rider_id):
        endpoint = f'rider/{rider_id}.json'
        return self._fetch_json(endpoint)

    def get_team(self, team_id):
        endpoint = f'team/{team_id}.json'
        return self._fetch_json(endpoint)

    def get_latest_results(self):
        return self._fetch_json('latestResults.php')

    def get_latest_results_today(self):
        return self._fetch_json('latestResultsTODAY.php')

    def sync_race_calendar(self, year, gender, commit=True):
        logger.info('Race calendar sync started: year=%s gender=%s', year, gender)
        payload, source_endpoint = self.get_race_calendar(season_year=year)

        race_rows = self._extract_race_calendar_rows(payload)
        logger.info('Race calendar payload parsed: year=%s rows=%s', year, len(race_rows))
        synced = 0
        skipped = 0
        filtered_out = 0

        for idx, row in enumerate(race_rows, start=1):
            if not isinstance(row, dict):
                skipped += 1
                continue

            if not _gender_matches(row.get('gender'), gender):
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

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Race calendar progress: year=%s processed=%s/%s synced=%s skipped=%s filtered_out=%s',
                    year,
                    idx,
                    len(race_rows),
                    synced,
                    skipped,
                    filtered_out,
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Race calendar sync finished: year=%s gender=%s requested=%s synced=%s skipped=%s filtered_out=%s',
            year,
            gender,
            len(race_rows),
            synced,
            skipped,
            filtered_out,
        )

        return {
            'requested': len(race_rows),
            'synced': synced,
            'skipped': skipped,
            'filtered_out': filtered_out,
        }

    def sync_teams(self, year, gender, limit=None, commit=True):
        logger.info(
            'Team sync started: year=%s gender=%s limit=%s method=%s fetch_only_missing=%s',
            year,
            gender,
            limit,
            self.sync_method,
            self.fetch_only_missing,
        )
        payload, source_endpoint = self.get_team_overview(season_year=year, gender=gender)

        team_rows = self._extract_team_overview_rows(payload)
        team_ids = []
        filtered_out = 0
        skipped = 0

        for row in team_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue

            if not _gender_matches(row.get('gender'), gender):
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

        logger.info(
            'Team overview parsed: year=%s gender=%s candidates=%s skipped=%s filtered_out=%s',
            year,
            gender,
            len(team_ids),
            skipped,
            filtered_out,
        )

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            team_ids = team_ids[:limit]

        skipped_existing = 0
        if self.fetch_only_missing:
            original_count = len(team_ids)
            team_ids, existing_count = self._filter_missing_team_ids(team_ids)
            skipped_existing = existing_count
            logger.info(
                'Team sync missing filter applied: year=%s gender=%s existing=%s remaining=%s',
                year,
                gender,
                existing_count,
                len(team_ids),
            )

        fetched = 0
        upserted = 0
        failures = []
        normalized_rows = []

        for idx, team_id in enumerate(team_ids, start=1):
            try:
                team_payload, team_source_endpoint = self.get_team(team_id)
                if self.sync_method == 'incremental_upsert':
                    self._upsert_team(
                        team_id=team_id,
                        payload=team_payload,
                        source_endpoint=team_source_endpoint,
                    )
                    upserted += 1
                else:
                    normalized_rows.append(
                        self._team_record_from_payload(
                            team_id=team_id,
                            payload=team_payload,
                            source_endpoint=team_source_endpoint,
                        )
                    )
                fetched += 1
            except Exception as exc:
                failures.append((team_id, str(exc)))
                logger.warning('Team sync failure: team_id=%s error=%s', team_id, exc)

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Team sync progress: year=%s gender=%s processed=%s/%s fetched=%s failed=%s',
                    year,
                    gender,
                    idx,
                    len(team_ids),
                    fetched,
                    len(failures),
                )

        applied = False
        applied_mode = None
        if self.sync_method == 'incremental_upsert':
            applied = upserted > 0
            applied_mode = 'incremental_upsert' if applied else None
            logger.info(
                'Team incremental upsert finished: year=%s gender=%s upserted=%s failed=%s',
                year,
                gender,
                upserted,
                len(failures),
            )
        else:
            if not failures:
                # Refresh collected teams only after complete successful collection.
                db_load_started = time.perf_counter()
                delete_query = self.db_session.query(FirstCyclingTeam)
                if team_ids:
                    delete_query = delete_query.filter(FirstCyclingTeam.team_id.in_(team_ids))
                delete_count = delete_query.delete(synchronize_session=False)

                if self.verbose_sync:
                    logger.info(
                        'Team refresh delete: year=%s gender=%s deleted=%s',
                        year,
                        gender,
                        delete_count,
                    )

                team_mappings = [
                    self._to_model_kwargs(FirstCyclingTeam, row)
                    for row in normalized_rows
                ]
                if team_mappings:
                    self.db_session.execute(FirstCyclingTeam.__table__.insert(), team_mappings)
                applied = True
                applied_mode = 'bulk_refresh'

                logger.info(
                    'Team DB load finished: year=%s gender=%s rows=%s deleted=%s duration_ms=%s',
                    year,
                    gender,
                    len(team_mappings),
                    delete_count,
                    int((time.perf_counter() - db_load_started) * 1000),
                )
            elif normalized_rows:
                for row in normalized_rows:
                    self._upsert_team_record(team_id=row['team_id'], normalized=row)
                applied = True
                applied_mode = 'partial_upsert'
                logger.warning(
                    'Team refresh fell back to partial upsert due to collection failures: year=%s gender=%s applied=%s failures=%s',
                    year,
                    gender,
                    len(normalized_rows),
                    len(failures),
                )
            else:
                logger.warning(
                    'Team refresh skipped due to collection failures with no collected rows: year=%s gender=%s failures=%s',
                    year,
                    gender,
                    len(failures),
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Team sync finished: year=%s gender=%s requested=%s fetched=%s failed=%s skipped=%s filtered_out=%s skipped_existing=%s method=%s',
            year,
            gender,
            len(team_ids),
            fetched,
            len(failures),
            skipped,
            filtered_out,
            skipped_existing,
            self.sync_method,
        )

        return {
            'requested': len(team_ids),
            'fetched': fetched,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'skipped_existing': skipped_existing,
            'failures': failures,
            'applied': applied,
            'method': self.sync_method,
            'applied_mode': applied_mode,
        }

    def sync_riders(self, year, gender, limit=None, commit=True):
        logger.info(
            'Rider sync started: year=%s gender=%s limit=%s method=%s fetch_only_missing=%s',
            year,
            gender,
            limit,
            self.sync_method,
            self.fetch_only_missing,
        )
        rider_ids, skipped, filtered_out = self._collect_rider_ids_from_overview(year=year, gender=gender)

        logger.info(
            'Rider IDs loaded from rider overview: year=%s gender=%s candidates=%s skipped=%s filtered_out=%s',
            year,
            gender,
            len(rider_ids),
            skipped,
            filtered_out,
        )

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            rider_ids = rider_ids[:limit]

        skipped_existing = 0
        if self.fetch_only_missing:
            rider_ids, existing_count = self._filter_missing_rider_ids(rider_ids)
            skipped_existing = existing_count
            logger.info(
                'Rider sync missing filter applied: year=%s gender=%s existing=%s remaining=%s',
                year,
                gender,
                existing_count,
                len(rider_ids),
            )

        fetched = 0
        upserted = 0
        failures = []
        normalized_rows = []
        collected_rider_ids = []

        for idx, rider_id in enumerate(rider_ids, start=1):
            try:
                rider_payload, rider_source_endpoint = self.get_rider(rider_id)
                if self.sync_method == 'incremental_upsert':
                    self._upsert_rider(
                        rider_id=rider_id,
                        payload=rider_payload,
                        source_endpoint=rider_source_endpoint,
                        season_year=year,
                    )
                    upserted += 1
                else:
                    normalized_rows.append(
                        self._rider_record_from_payload(
                            rider_id=rider_id,
                            payload=rider_payload,
                            source_endpoint=rider_source_endpoint,
                            season_year=year,
                        )
                    )
                    collected_rider_ids.append(rider_id)
                fetched += 1
            except Exception as exc:
                failures.append((rider_id, str(exc)))
                logger.warning('Rider sync failure: rider_id=%s error=%s', rider_id, exc)

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Rider sync progress: year=%s gender=%s processed=%s/%s fetched=%s failed=%s',
                    year,
                    gender,
                    idx,
                    len(rider_ids),
                    fetched,
                    len(failures),
                )

        applied = False
        applied_mode = None
        if self.sync_method == 'incremental_upsert':
            applied = upserted > 0
            applied_mode = 'incremental_upsert' if applied else None
            logger.info(
                'Rider incremental upsert finished: year=%s gender=%s upserted=%s failed=%s',
                year,
                gender,
                upserted,
                len(failures),
            )
        else:
            if not failures:
                # Refresh collected rider IDs only after complete successful collection.
                db_load_started = time.perf_counter()
                delete_query = self.db_session.query(FirstCyclingRider)
                if collected_rider_ids:
                    delete_query = delete_query.filter(FirstCyclingRider.rider_id.in_(collected_rider_ids))

                delete_count = delete_query.delete(synchronize_session=False)
                if self.verbose_sync:
                    logger.info(
                        'Rider refresh delete: year=%s gender=%s deleted=%s',
                        year,
                        gender,
                        delete_count,
                    )

                rider_mappings = [
                    self._to_model_kwargs(FirstCyclingRider, row)
                    for row in normalized_rows
                ]
                if rider_mappings:
                    self.db_session.execute(FirstCyclingRider.__table__.insert(), rider_mappings)
                applied = True
                applied_mode = 'bulk_refresh'

                logger.info(
                    'Rider DB load finished: year=%s gender=%s rows=%s deleted=%s duration_ms=%s',
                    year,
                    gender,
                    len(rider_mappings),
                    delete_count,
                    int((time.perf_counter() - db_load_started) * 1000),
                )
            elif normalized_rows:
                for row in normalized_rows:
                    self._upsert_rider_record(rider_id=row['rider_id'], normalized=row)
                applied = True
                applied_mode = 'partial_upsert'
                logger.warning(
                    'Rider refresh fell back to partial upsert due to collection failures: year=%s gender=%s applied=%s failures=%s',
                    year,
                    gender,
                    len(normalized_rows),
                    len(failures),
                )
            else:
                logger.warning(
                    'Rider refresh skipped due to collection failures: year=%s gender=%s failures=%s',
                    year,
                    gender,
                    len(failures),
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Rider sync finished: year=%s gender=%s requested=%s fetched=%s failed=%s skipped=%s filtered_out=%s skipped_existing=%s method=%s',
            year,
            gender,
            len(rider_ids),
            fetched,
            len(failures),
            skipped,
            filtered_out,
            skipped_existing,
            self.sync_method,
        )

        return {
            'requested': len(rider_ids),
            'fetched': fetched,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'skipped_existing': skipped_existing,
            'failures': failures,
            'applied': applied,
            'method': self.sync_method,
            'applied_mode': applied_mode,
        }

    def sync_race_results_window(self, year, gender, start_date, end_date, commit=True):
        start = self._parse_input_date(start_date, 'start_date')
        end = self._parse_input_date(end_date, 'end_date')
        if start > end:
            raise ValueError('start_date must be on or before end_date')

        logger.info(
            'Race results window sync started: year=%s gender=%s start=%s end=%s',
            year,
            gender,
            start,
            end,
        )

        candidate_races = self.db_session.query(FirstCyclingRace).filter(
            FirstCyclingRace.season_year == year,
            FirstCyclingRace.start_date.isnot(None),
        ).all()

        races_in_scope = []
        skipped_no_dates = 0
        skipped_gender = 0

        for race in candidate_races:
            if not _gender_matches(race.gender, gender):
                skipped_gender += 1
                continue

            race_start = race.start_date
            race_end = race.end_date or race.start_date
            if race_start is None:
                skipped_no_dates += 1
                continue

            if self._date_windows_overlap(race_start, race_end, start, end):
                races_in_scope.append(race)

        logger.info(
            'Race results scope resolved: year=%s gender=%s candidates=%s in_scope=%s skipped_gender=%s skipped_no_dates=%s',
            year,
            gender,
            len(candidate_races),
            len(races_in_scope),
            skipped_gender,
            skipped_no_dates,
        )

        synced = 0
        skipped = 0
        failed = 0
        total_rows_synced = 0
        failures = []

        for idx, race in enumerate(races_in_scope, start=1):
            try:
                result_summary = self.sync_race_results(
                    race_id=race.race_id,
                    season_year=year,
                    commit=False,
                )
                if result_summary.get('skipped_non_road_race'):
                    skipped += 1
                else:
                    synced += 1
                    total_rows_synced += int(result_summary.get('synced') or 0)
            except Exception as exc:
                failed += 1
                failures.append((race.race_id, str(exc)))
                logger.warning('Race results sync failure: race_id=%s error=%s', race.race_id, exc)

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Race results window progress: year=%s gender=%s processed=%s/%s synced=%s skipped=%s failed=%s',
                    year,
                    gender,
                    idx,
                    len(races_in_scope),
                    synced,
                    skipped,
                    failed,
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Race results window sync finished: year=%s gender=%s start=%s end=%s requested=%s synced=%s skipped=%s failed=%s rows_synced=%s',
            year,
            gender,
            start,
            end,
            len(races_in_scope),
            synced,
            skipped,
            failed,
            total_rows_synced,
        )

        return {
            'requested': len(races_in_scope),
            'synced': synced,
            'skipped': skipped,
            'failed': failed,
            'rows_synced': total_rows_synced,
            'failures': failures,
            'start_date': str(start),
            'end_date': str(end),
            'candidates': len(candidate_races),
            'skipped_no_dates': skipped_no_dates,
            'skipped_gender': skipped_gender,
        }

    def sync_latest_results(self, season_year, only_today=False, commit=True):
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
            season_year=season_year,
            commit=False,
        )
        if commit:
            self.db_session.commit()
        return snapshot

    def sync_race_results(self, race_id, season_year, commit=True):
        race_id = self._parse_int(race_id)
        if race_id is None:
            raise ValueError('race_id must be an integer')

        payload, source_endpoint = self.get_race(race_id=race_id, season_year=season_year)
        race_details = payload.get('info')

        race = None
        if race_details:
            race = self._upsert_race(
                race_id=race_id,
                season_year=season_year,
                payload=race_details,
                source_endpoint=source_endpoint,
            )
            

        if race is None:
            if commit:
                self.db_session.commit()
            return {
                'season_year': season_year,
                'race_id': race_id,
                'synced': 0,
                'deleted_existing': 0,
                'overall_results': 0,
                'stage_results': 0,
                'skipped_riders': 0,
                'skipped_stage_rows': 0,
                'skipped_non_road_race': True,
            }

        self.db_session.flush()

        stage_rows = self.db_session.query(FirstCyclingRaceStage).filter_by(race_id=race.id).all()
        stage_by_number = {
            stage.stage_number: stage
            for stage in stage_rows
            if stage.stage_number is not None
        }

        deleted_existing = self.db_session.query(FirstCyclingRaceResult).filter_by(
            race_id=race.id
        ).delete(synchronize_session=False)

        rider_rows = self._extract_riders_result_rows(payload)
        results_to_insert = []
        overall_results = 0
        stage_results = 0
        skipped_riders = 0
        skipped_stage_rows = 0

        for rider_key, rider_row in rider_rows.items():
            if not isinstance(rider_row, dict):
                skipped_riders += 1
                continue

            rider_id = self._parse_int(rider_row.get('id')) or self._parse_int(rider_key)
            if rider_id is None:
                skipped_riders += 1
                continue

            self._upsert_rider(
                rider_id=rider_id,
                payload=rider_row,
                source_endpoint=source_endpoint,
                season_year=season_year,
            )

            team_id = self._parse_int(rider_row.get('teamId'))
            is_still_in_race = self._parse_is_still_in_race(rider_row.get('stillInTheRace'))

            overall = rider_row.get('overall')
            overall_position = None
            overall_time = None
            overall_time_seconds = None
            if isinstance(overall, dict):
                overall_position = self._parse_int(overall.get('position'))
                overall_time = overall.get('time')
                overall_time_seconds = self._parse_int(overall.get('timeSeconds'))

            rider_stage_payloads = []
            for stage_key, stage_payload in rider_row.items():
                stage_number = self._stage_number_from_key(stage_key)
                if stage_number is None or not isinstance(stage_payload, dict):
                    continue

                rider_stage_payloads.append((stage_number, stage_payload))

            if not rider_stage_payloads:
                results_to_insert.append(FirstCyclingRaceResult(
                    race_id=race.id,
                    stage_id=None,
                    result_scope='race',
                    rider_id=rider_id,
                    team_id=team_id,
                    overall_position=overall_position,
                    overall_time=overall_time,
                    overall_time_seconds=overall_time_seconds,
                    stage_position=None,
                    stage_time=None,
                    stage_time_seconds=None,
                    is_still_in_race=is_still_in_race,
                ))
                overall_results += 1
                continue

            for stage_number, stage_payload in rider_stage_payloads:

                stage = stage_by_number.get(stage_number)
                if stage is None:
                    skipped_stage_rows += 1
                    continue

                stage_position = self._parse_int(stage_payload.get('stagePos'))
                stage_time = stage_payload.get('stageTime')
                stage_time_seconds = self._parse_int(stage_payload.get('stageTimeSeconds'))

                row_overall_position = self._parse_int(stage_payload.get('overallPos'))
                if row_overall_position is None:
                    row_overall_position = overall_position

                row_overall_time = stage_payload.get('overallTime')
                if row_overall_time is None:
                    row_overall_time = overall_time

                row_overall_time_seconds = self._parse_int(stage_payload.get('overallTimeSeconds'))
                if row_overall_time_seconds is None:
                    row_overall_time_seconds = overall_time_seconds

                results_to_insert.append(FirstCyclingRaceResult(
                    race_id=race.id,
                    stage_id=stage.id,
                    result_scope='stage',
                    rider_id=rider_id,
                    team_id=team_id,
                    overall_position=row_overall_position,
                    overall_time=row_overall_time,
                    overall_time_seconds=row_overall_time_seconds,
                    stage_position=stage_position,
                    stage_time=stage_time,
                    stage_time_seconds=stage_time_seconds,
                    is_still_in_race=is_still_in_race,
                ))
                stage_results += 1

        if results_to_insert:
            self.db_session.add_all(results_to_insert)

        if commit:
            self.db_session.commit()

        return {
            'season_year': season_year,
            'race_id': race_id,
            'synced': len(results_to_insert),
            'deleted_existing': deleted_existing,
            'overall_results': overall_results,
            'stage_results': stage_results,
            'skipped_riders': skipped_riders,
            'skipped_stage_rows': skipped_stage_rows,
            'skipped_non_road_race': False,
        }

    def sync_season_roster(self, season_year, gender, commit=True):
        """Sync race calendar, teams, and riders for a season.

        Steps:
        1. Fetch and snapshot race calendar, team overview, and rider overview feeds.
        2. Upsert race rows from race calendar (excluding CYCLOCROSS races).
        3. Fetch each team detail payload and bulk upsert into database.
        4. Fetch each rider detail payload and bulk upsert into database.
        """

        race_summary = self.sync_race_calendar(year=season_year, gender=gender, commit=False)
        team_summary = self.sync_teams(year=season_year, gender=gender, commit=False)
        rider_summary = self.sync_riders(year=season_year, gender=gender, commit=False)

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
