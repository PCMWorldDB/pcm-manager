"""ProCyclingStats parsing and FirstCycling rider matching."""
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised only when dependency is missing
    BeautifulSoup = None

from webapp.extensions import db
from webapp.models.main.firstcycling import FirstCyclingRider
from webapp.models.main.pcs import PcsRaceResult

logger = logging.getLogger('pcs')


class PcsParseError(Exception):
    """Raised when PCS result HTML cannot be parsed."""


@dataclass
class PcsRaceResultRow:
    """Parsed PCS result row before database persistence."""

    position: int | None
    rider_name: str
    team_name: str | None = None
    age: int | None = None
    points: int | None = None
    time: str | None = None


@dataclass
class PcsRaceResultParse:
    """Parsed PCS result page details and rows."""

    race_name: str
    rows: list[PcsRaceResultRow]


@dataclass
class PcsRiderMatch:
    """Result of matching one PCS rider name to a FirstCycling rider."""

    rider_id: int | None
    method: str | None
    score: float | None
    rider: FirstCyclingRider | None = None


def _parse_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _clean_text(value):
    if value is None:
        return None
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return text or None


def _title_from_slug(value):
    slug = _clean_text(value)
    if not slug:
        return None
    words = re.sub(r'[-_]+', ' ', slug).split()
    if not words:
        return None
    lowercase_words = {'de', 'del', 'la', 'las', 'le', 'les', 'of', 'the'}
    titled_words = []
    for index, word in enumerate(words):
        if index > 0 and word.lower() in lowercase_words:
            titled_words.append(word.lower())
        else:
            titled_words.append(word[:1].upper() + word[1:])
    return ' '.join(titled_words)


def _normalize_name(value):
    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r'[^A-Za-z0-9]+', ' ', text).strip().lower()
    return re.sub(r'\s+', ' ', text)


def _name_variants(value):
    normalized = _normalize_name(value)
    if not normalized:
        return []

    tokens = normalized.split()
    variants = {normalized}
    if len(tokens) > 1:
        variants.add(' '.join(tokens[1:] + tokens[:1]))
        variants.add(' '.join(tokens[-1:] + tokens[:-1]))
        variants.add(' '.join(sorted(tokens)))

    return [variant for variant in variants if variant]


def _best_name_score(pcs_name, firstcycling_name):
    pcs_variants = _name_variants(pcs_name)
    fc_variants = _name_variants(firstcycling_name)
    if not pcs_variants or not fc_variants:
        return 0.0

    best_score = 0.0
    for pcs_variant in pcs_variants:
        pcs_tokens = set(pcs_variant.split())
        for fc_variant in fc_variants:
            fc_tokens = set(fc_variant.split())
            sequence_score = SequenceMatcher(None, pcs_variant, fc_variant).ratio()
            token_score = 0.0
            if pcs_tokens and fc_tokens:
                token_score = len(pcs_tokens & fc_tokens) / len(pcs_tokens | fc_tokens)
            best_score = max(best_score, sequence_score, token_score)
    return best_score


def _cell_by_code(row, code):
    return row.select_one(f'td[data-code="{code}"], th[data-code="{code}"]')


def _row_cells_by_header(row, headers):
    cells = row.find_all('td', recursive=False)
    return {
        header: cells[index]
        for index, header in enumerate(headers)
        if index < len(cells)
    }


def _parse_result_table_headers(table):
    header_cells = table.select('thead th')
    headers = []
    for cell in header_cells:
        code = _clean_text(cell.get('data-code'))
        if not code:
            code = _normalize_name(cell.get_text(' ', strip=True))
        headers.append(code)
    return headers


def _extract_pcs_url(soup, source_url=None):
    if source_url:
        return source_url

    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical:
        return canonical.get('href')

    saved_from = soup.find(string=re.compile(r'saved from url='))
    if saved_from:
        match = re.search(r'https?://[^\s)]+', saved_from)
        if match:
            return match.group(0)

    return None


def _race_name_from_pcs_url(pcs_url):
    if not pcs_url:
        return None

    path_parts = [part for part in urlparse(pcs_url).path.split('/') if part]
    if len(path_parts) < 2:
        return None

    race_slug = path_parts[1] if path_parts[0] in ('race', 'national-race') else path_parts[0]
    return _title_from_slug(re.sub(r'\d+$', '', race_slug))


def _extract_race_name(soup, source_url=None):
    race_name = _race_name_from_pcs_url(_extract_pcs_url(soup, source_url=source_url))
    if race_name:
        return race_name

    title = soup.find('title')
    title_text = _clean_text(title.get_text(' ', strip=True)) if title else None
    if title_text:
        return re.sub(r'\s+\d{4}\b.*$', '', title_text).strip() or title_text

    raise PcsParseError('Could not determine PCS race name')


def _extract_time(cell):
    if cell is None:
        return None

    hidden = cell.select_one('.hide')
    hidden_time = _clean_text(hidden.get_text(' ', strip=True)) if hidden else None

    visible_source = cell.select_one('font') or cell
    time_text = _clean_text(visible_source.get_text(' ', strip=True))
    if not time_text or time_text == ',,':
        return hidden_time
    return time_text


def _extract_rider_name(rider_cell, rider_link):
    if rider_link:
        return _clean_text(rider_link.get_text(' ', strip=True))
    if rider_cell is None:
        return None

    for mobile_team in rider_cell.select('.showIfMobile'):
        mobile_team.decompose()
    for flag in rider_cell.select('.flag'):
        flag.decompose()
    return _clean_text(rider_cell.get_text(' ', strip=True))


def parse_pcs_race_results_html(html, source_url=None):
    """Parse PCS race result HTML into structured page data."""
    if BeautifulSoup is None:
        raise PcsParseError('beautifulsoup4 is required to parse PCS race result HTML')

    soup = BeautifulSoup(html or '', 'html.parser')
    race_name = _extract_race_name(soup, source_url=source_url)
    table = soup.select_one('table.results')
    if table is None:
        raise PcsParseError('Could not find PCS results table')

    headers = _parse_result_table_headers(table)
    parsed_rows = []
    for row in table.select('tbody tr'):
        cells = _row_cells_by_header(row, headers)
        position_cell = _cell_by_code(row, 'rnk') or cells.get('rnk')
        rider_cell = _cell_by_code(row, 'ridername') or cells.get('ridername')
        team_cell = _cell_by_code(row, 'teamnamelink') or cells.get('teamnamelink')
        age_cell = _cell_by_code(row, 'age') or cells.get('age')
        points_cell = _cell_by_code(row, 'pnt') or cells.get('pnt')
        time_cell = _cell_by_code(row, 'time') or cells.get('time')

        rider_link = rider_cell.select_one('a[href*="/rider/"]') if rider_cell else None
        rider_name = _extract_rider_name(rider_cell, rider_link)
        if not rider_name:
            continue

        team_link = team_cell.select_one('a') if team_cell else None

        position_text = _clean_text(position_cell.get_text(' ', strip=True)) if position_cell else None

        parsed_rows.append(PcsRaceResultRow(
            position=_parse_int(position_text),
            rider_name=rider_name,
            team_name=_clean_text(team_link.get_text(' ', strip=True)) if team_link else None,
            age=_parse_int(age_cell.get_text(' ', strip=True)) if age_cell else None,
            points=_parse_int(points_cell.get_text(' ', strip=True)) if points_cell else None,
            time=_extract_time(time_cell),
        ))

    logger.info('Parsed %s PCS result rows from %s', len(parsed_rows), source_url or 'HTML')
    return PcsRaceResultParse(race_name=race_name, rows=parsed_rows)


class PcsRiderMatcher:
    """Match PCS rider names to FirstCycling riders."""

    def __init__(self, db_session=None, fuzzy_threshold=0.84):
        self.db_session = db_session or db.session
        self.fuzzy_threshold = fuzzy_threshold

    def match(self, pcs_name):
        pcs_name = _clean_text(pcs_name)
        if not pcs_name:
            return PcsRiderMatch(rider_id=None, method=None, score=None)

        exact_pcs_name = (
            self.db_session.query(FirstCyclingRider)
            .filter(FirstCyclingRider.pcs_name == pcs_name)
            .order_by(FirstCyclingRider.rider_id.asc())
            .first()
        )
        if exact_pcs_name:
            return PcsRiderMatch(
                rider_id=exact_pcs_name.rider_id,
                method='pcs_name_exact',
                score=1.0,
                rider=exact_pcs_name,
            )

        best_rider = None
        best_score = 0.0
        riders = (
            self.db_session.query(FirstCyclingRider)
            .filter(FirstCyclingRider.name.isnot(None))
            .all()
        )
        for rider in riders:
            score = _best_name_score(pcs_name, rider.name)
            if score > best_score:
                best_score = score
                best_rider = rider

        if best_rider and best_score >= self.fuzzy_threshold:
            return PcsRiderMatch(
                rider_id=best_rider.rider_id,
                method='firstcycling_name_fuzzy',
                score=best_score,
                rider=best_rider,
            )

        return PcsRiderMatch(
            rider_id=None,
            method='unmatched',
            score=best_score if best_score else None,
            rider=best_rider,
        )


class PcsClient:
    """Persistence helper for parsed PCS race results."""

    def __init__(self, db_session=None, fuzzy_threshold=0.84):
        self.db_session = db_session or db.session
        self.matcher = PcsRiderMatcher(self.db_session, fuzzy_threshold=fuzzy_threshold)

    def sync_race_results_from_html(
        self,
        *,
        html,
        source_url=None,
        commit=True,
    ):
        parsed = parse_pcs_race_results_html(html, source_url=source_url)
        deleted_existing = (
            self.db_session.query(PcsRaceResult)
            .filter_by(race_name=parsed.race_name)
            .delete(synchronize_session=False)
        )

        rows_to_insert = []
        matched = 0
        for parsed_row in parsed.rows:
            match = self.matcher.match(parsed_row.rider_name)
            if match.rider_id is not None:
                matched += 1

            row_payload = asdict(parsed_row)
            rows_to_insert.append(PcsRaceResult(
                race_name=parsed.race_name,
                matched_firstcycling_rider_id=match.rider_id,
                match_method=match.method,
                match_score=match.score,
                last_synced_at=datetime.utcnow(),
                **row_payload,
            ))

        if rows_to_insert:
            self.db_session.add_all(rows_to_insert)
        if commit:
            self.db_session.commit()

        return {
            'race_name': parsed.race_name,
            'parsed': len(parsed.rows),
            'inserted': len(rows_to_insert),
            'matched': matched,
            'unmatched': len(parsed.rows) - matched,
            'deleted_existing': deleted_existing,
        }


__all__ = [
    'PcsClient',
    'PcsParseError',
    'PcsRaceResultParse',
    'PcsRaceResultRow',
    'PcsRiderMatch',
    'PcsRiderMatcher',
    'parse_pcs_race_results_html',
]
