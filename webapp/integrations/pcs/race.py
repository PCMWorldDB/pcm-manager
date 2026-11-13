"""PCS race URL normalization, offline parsing and transactional persistence."""
from dataclasses import asdict
from datetime import date, datetime
from hashlib import sha256
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert

from webapp.extensions import db
from webapp.integrations.pcs import PcsParseError, _normalize_pcs_rider_path, parse_pcs_race_results_html
from webapp.integrations.pcs.slug import rider_profile_url
from webapp.models.main.identity import RaceIdentity
from webapp.models.main.pcs import PcsRace, PcsRaceResult, PcsRider, PcsRiderResult

RACE_ENDPOINT = 'get_race'
PCS_BASE = 'https://www.procyclingstats.com'


def parse_race_url(value, year):
    """Accept PCS landing, startlist, flat and slash-separated edition URLs."""
    try:
        if type(year) is not int or not 1 <= year <= 9999:
            raise ValueError
        url = urlsplit(value.strip())
        parts = url.path.strip('/').split('/')
        if (url.scheme not in {'http', 'https'}
                or url.netloc.lower() not in {'www.procyclingstats.com', 'procyclingstats.com'}
                or len(parts) < 2 or parts[0] != 'race'
                or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', parts[1]) or len(parts[1]) > 500):
            raise ValueError
        slug, suffix = parts[1], parts[2:]
        flat = re.fullmatch(r'(.+)-(\d{4})(?:-(stage-\d+|gc|result))?', slug)
        scope = None
        if flat:
            slug, edition, scope = flat.groups()
            if int(edition) != year:
                raise ValueError
        elif suffix and suffix[0].isdigit():
            if suffix.pop(0) != str(year):
                raise ValueError
        if scope:
            if suffix not in ([], ['result'], ['results'], ['results', 'results']):
                raise ValueError
        elif suffix in (['startlist'], ['startlist', 'startlist']):
            scope = 'startlist/startlist'
        elif suffix:
            if len(suffix) != 1 or not re.fullmatch(r'results?|gc|stage-\d+', suffix[0]):
                raise ValueError
            scope = 'result' if suffix[0] == 'results' else suffix[0]
    except (ValueError, AttributeError, IndexError, TypeError):
        raise ValueError('Enter a ProCyclingStats race URL for the selected namespace season.') from None
    race_url = f'{PCS_BASE}/race/{slug}/{year}' + (f'/{scope}' if scope else '')
    return dict(race_slug=slug, season_year=year, race_url=race_url)


def parse_pcs_race_html(html, *, source_url, year, gender):
    """Parse one fetched page; startlists never become confirmed results."""
    identity = parse_race_url(source_url, year)
    soup = BeautifulSoup(html or '', 'html.parser')
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical:
        actual = parse_race_url(canonical['href'], year)
        if actual['race_slug'] != identity['race_slug']:
            raise PcsParseError('PCS returned a different race edition.')
        # A landing URL can resolve to a stage or GC page for this edition.
        if identity['race_url'].endswith(str(year)):
            identity = actual
        elif actual['race_url'] != identity['race_url']:
            raise PcsParseError('PCS returned a different race result page.')
    heading = soup.select_one('h1')
    if heading is None:
        raise PcsParseError('Could not find PCS race heading.')
    name = ' '.join(text.strip() for text in heading.find_all(string=True, recursive=False) if text.strip())
    if not name:
        name = re.sub(r'^\d{4}\s*', '', heading.get_text(' ', strip=True))
    classification = re.search(r'\(([^()]+)\)', heading.get_text(' ', strip=True))
    payload = dict(**identity, gender=gender, name=name,
                   classification=classification.group(1) if classification else None)
    info = {}
    for item in soup.select('.infolist li'):
        parts = item.find_all('div', recursive=False)
        if len(parts) >= 2:
            info[parts[0].get_text(' ', strip=True).rstrip(':').lower()] = parts[1].get_text(' ', strip=True)
    for label in soup.select('.lineh16 > div.bold'):
        value = label.find_next_sibling('div')
        if value:
            info[label.get_text(' ', strip=True).rstrip(':').lower()] = value.get_text(' ', strip=True)
    category = info.get('race category')
    actual_gender = {'ME': 'Men', 'WE': 'Women', 'MJ': 'Men', 'WJ': 'Women'}.get(category)
    if actual_gender and actual_gender != gender:
        raise PcsParseError('PCS returned a different race gender.')
    payload['category'] = category
    payload['info'] = info
    result_date = None
    raw_date = info.get('date')
    if raw_date:
        for pattern in ('%d %B %Y', '%d %b %Y', '%Y-%m-%d', '%d.%m.%Y'):
            try:
                result_date = datetime.strptime(raw_date, pattern).date().isoformat()
                break
            except ValueError:
                continue
    if result_date and date.fromisoformat(result_date).year != year:
        raise PcsParseError('PCS returned a different race edition.')
    scope = identity['race_url'].split(f'/{year}', 1)[1].strip('/')
    stage = re.fullmatch(r'stage-(\d+)', scope)
    payload.update(result_date=result_date, scope=scope, stage_number=int(stage[1]) if stage else None)
    stages = set()
    for option in soup.select('select option[value]'):
        candidate = option['value']
        if candidate.startswith('race/'):
            candidate = f'{PCS_BASE}/{candidate}'
        try:
            linked = parse_race_url(candidate, year)
        except ValueError:
            continue
        number = re.search(r'/stage-(\d+)$', linked['race_url'])
        if linked['race_slug'] == identity['race_slug'] and number:
            stages.add(int(number[1]))
    if stages:
        payload['stage_count'] = len(stages)
    if stage and info.get('departure') and info.get('arrival'):
        payload['stage_name'] = f"{info['departure']} › {info['arrival']}"
    # Stage facts belong in the page payload, not the edition's overall distance/dates.
    if not stage and scope != 'gc':
        if result_date:
            payload.update(start_date=result_date, end_date=result_date)
        distance = re.search(r'[\d.]+', info.get('distance', ''))
        if distance:
            payload['distance_km'] = float(distance[0])
        payload.update(start_location=info.get('departure'), end_location=info.get('arrival'))
    if scope.startswith('startlist'):
        payload['rows'] = []
        return payload
    table = soup.select_one('table.results')
    if table is None:
        # A recognizable race overview can have no results yet.
        if not info and not soup.select_one('.page-title'):
            raise PcsParseError('Could not find PCS race facts or results.')
        if stage or scope in {'result', 'gc'}:
            raise PcsParseError('Could not find PCS results table.')
        payload['rows'] = []
        return payload
    payload['rows'] = [asdict(row) for row in parse_pcs_race_results_html(html, source_url).rows]
    if not payload['rows']:
        raise PcsParseError('PCS results table contains no recognizable riders.')
    if not scope:
        payload['scope'] = 'gc' if classification and classification[1].startswith('2.') else 'result'
        payload['race_url'] += '/' + payload['scope']
    return payload


def fetch_race(params):
    """Use the queue's bounded PCS HTTP adapter and retry policy."""
    from webapp.worker import fetch_pcs_html

    html = fetch_pcs_html(params['race_url'])
    return parse_pcs_race_html(html, source_url=params['race_url'],
                               year=params['season_year'], gender=params['gender'])


def store_race(params, payload, *, namespace_id=None, db_session=None):
    """Upsert race and results together; the caller owns commit/rollback."""
    session = db_session if db_session is not None else db.session
    if not isinstance(payload, dict) or not isinstance(payload.get('name'), str) or not payload['name'].strip():
        raise ValueError('PCS did not return a race name.')
    if payload.get('race_slug') != params['race_slug'] or payload.get('season_year') != params['season_year']:
        raise ValueError('PCS returned a different race edition.')
    if payload.get('gender', params['gender']) != params['gender']:
        raise ValueError('PCS returned a different race gender.')
    fields = ('name', 'category', 'classification', 'nation_name', 'description', 'distance_km',
              'profile', 'start_date', 'end_date', 'start_location', 'end_location', 'stage_count')
    values = {field: payload[field] for field in fields if payload.get(field) is not None}
    for field in ('start_date', 'end_date'):
        if isinstance(values.get(field), str):
            values[field] = date.fromisoformat(values[field])
    now = datetime.utcnow()
    values.update(race_slug=params['race_slug'], season_year=params['season_year'], gender=params['gender'],
                  race_url=f"{PCS_BASE}/race/{params['race_slug']}/{params['season_year']}",
                  source_payload=payload, last_synced_at=now)
    race_id = session.execute(insert(PcsRace).values(**values).on_conflict_do_update(
        constraint='uq_pcs_race_edition', set_=values,
        where=PcsRace.gender == params['gender']).returning(PcsRace.id)).scalar_one_or_none()
    if race_id is None:
        raise ValueError('This PCS edition already belongs to a different gender.')
    session.execute(insert(RaceIdentity).values(pcs_slug=params['race_slug']).on_conflict_do_nothing(
        index_elements=[RaceIdentity.pcs_slug]))
    rows = payload.get('rows') or []
    if rows:
        _store_results(params, payload, rows, namespace_id, now, session)
    return dict(pcs_race_id=race_id, race_slug=params['race_slug'], season_year=params['season_year'],
                results=len(rows))


def _store_results(params, payload, rows, namespace_id, now, session):
    scope = payload['scope']
    if not re.fullmatch(r'result|gc|stage-\d+', scope):
        raise ValueError('Unsupported PCS results scope.')
    race_url = f"{PCS_BASE}/race/{params['race_slug']}/{params['season_year']}/{scope}"
    result_date = date.fromisoformat(payload['result_date']) if payload.get('result_date') else None
    row_type = 'stage_result' if scope.startswith('stage-') else 'classification' if scope == 'gc' else 'one_day_result'
    for row in rows:
        # The existing namespace table has no URL column. Use the canonical page
        # URL as its exact race key, keeping editions and stages independent.
        if namespace_id is not None:
            stored = session.query(PcsRaceResult).filter_by(namespace_id=namespace_id, race_name=race_url,
                                                  rider_name=row['rider_name']).first()
            if stored is None:
                stored = PcsRaceResult(namespace_id=namespace_id, race_name=race_url, rider_name=row['rider_name'])
                session.add(stored)
            for field in ('position', 'team_name', 'age', 'points', 'time'):
                setattr(stored, field, row.get(field))
            stored.last_synced_at = now
        rider_path = _normalize_pcs_rider_path(row.get('pcs_rider_path'))
        if not rider_path:
            continue
        slug = rider_path.split('/')[2]
        session.execute(insert(PcsRider).values(
            rider_slug=slug, profile_url=rider_profile_url(slug), display_name=row['rider_name'],
            last_synced_at=now).on_conflict_do_nothing(index_elements=[PcsRider.rider_slug]))
        rider_id = session.query(PcsRider.id).filter_by(rider_slug=slug).scalar()
        position = row.get('result_position') or str(row.get('position') or '')
        confirmed = row_type != 'classification' and (position.isdigit() or position in {'DNF', 'OTL', 'DSQ'})
        status = ('classification_only' if row_type == 'classification' else
                  'confirmed_participation' if confirmed else
                  'dns_only' if position == 'DNS' else 'not_confirmed')
        # Use the rider-profile key convention with a normalized race URL.
        key = sha256(repr((race_url, payload.get('result_date') or '', row_type, 1)).encode()).hexdigest()
        values = dict(season=params['season_year'], result_date=result_date,
                      race_id=f"{params['race_slug']}-{params['season_year']}", race_url=race_url,
                      race_name=payload['name'], race_category=payload.get('classification'),
                      stage_number=payload.get('stage_number'), stage_name=payload.get('stage_name'),
                      row_type=row_type, result_position=position,
                      participation_status=status,
                      confirmed_participation=confirmed, source_payload=row, last_synced_at=now)
        session.execute(insert(PcsRiderResult).values(rider_id=rider_id, source_result_key=key, **values)
                           .on_conflict_do_update(constraint='uq_pcs_rider_result_key', set_=values))
