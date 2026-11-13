"""Offline PCS race parsing and transactional result storage."""
from copy import deepcopy
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.integrations.pcs import PcsClient, PcsParseError
from webapp.integrations.pcs.race import parse_race_url, parse_pcs_race_html, store_race
from webapp.models import Namespace, PcsRace, PcsRaceResult, PcsRider, PcsRiderResult

BASE = 'https://www.procyclingstats.com/race/'
FIXTURE = Path(__file__).resolve().parents[1] / 'webapp/integrations/pcs/UAE Tour 2026 Stage 6 results.html'


@pytest.mark.parametrize('path,scope', [
    ('uae-tour', ''), ('uae-tour/startlist/startlist', '/startlist/startlist'),
    ('uae-tour-2026-stage-6/results', '/stage-6'), ('uae-tour-2026-gc', '/gc'),
    ('uae-tour/2026/gc', '/gc'), ('uae-tour/2026/stage-6', '/stage-6'),
])
def test_url_variants(path, scope):
    assert parse_race_url(BASE + path, 2026) == dict(
        race_slug='uae-tour', season_year=2026, race_url=BASE + 'uae-tour/2026' + scope)


@pytest.mark.parametrize('url', [
    BASE + 'uae-tour-2025-gc', BASE + 'uae-tour/2025/gc', BASE + 'uae-tour/garbage',
    'https://example.com/race/uae-tour', 'https://www.procyclingstats.com@evil.com/race/uae-tour',
    BASE + '../uae-tour', None,
])
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        parse_race_url(url, 2026)


def fixture_payload():
    return parse_pcs_race_html(FIXTURE.read_text(), source_url=BASE + 'uae-tour-2026-stage-6/results',
                               year=2026, gender='Men')


def test_supplied_stage_html():
    payload = fixture_payload()
    assert payload['race_slug'] == 'uae-tour'
    assert payload['name'] == 'UAE Tour'
    assert payload['classification'] == '2.UWT'
    assert payload['stage_number'] == 6
    assert payload['stage_count'] == 7
    assert payload['stage_name'] == 'Al Ain Museum › Jebel Hafeet'
    assert payload['result_date'] == '2026-02-21'
    assert len(payload['rows']) > 100
    first = payload['rows'][0]
    assert first['position'] == 1
    assert first['rider_name'] and first['team_name']
    assert first['age'] == 22 and first['points'] == 50 and first['time'] == '3:27:54'
    assert first['pcs_rider_path']
    assert 'start_date' not in payload and 'distance_km' not in payload


def test_startlist_and_invalid_pages():
    payload = parse_pcs_race_html('<h1>UAE Tour</h1>', source_url=BASE + 'uae-tour/startlist/startlist',
                                  year=2026, gender='Men')
    assert payload['rows'] == []
    for html in ('<h1>Access denied</h1>', '<html>Challenge</html>'):
        with pytest.raises(PcsParseError):
            parse_pcs_race_html(html, source_url=BASE + 'uae-tour-2026-stage-6/results', year=2026, gender='Men')
    with pytest.raises((ValueError, PcsParseError)):
        parse_pcs_race_html(FIXTURE.read_text(), source_url=BASE + 'other-race/2026/stage-6', year=2026, gender='Men')
    with pytest.raises(PcsParseError, match='gender'):
        parse_pcs_race_html(FIXTURE.read_text(), source_url=BASE + 'uae-tour-2026-stage-6/results', year=2026, gender='Women')


def test_fetch_adapter_parses_the_supplied_response(monkeypatch):
    from webapp import worker
    from webapp.integrations.pcs.race import fetch_race
    calls = []
    monkeypatch.setattr(worker, 'fetch_pcs_html', lambda url: calls.append(url) or FIXTURE.read_bytes())
    params = dict(parse_race_url(BASE + 'uae-tour-2026-stage-6/results', 2026), gender='Men')
    assert fetch_race(params)['rows'][0]['position'] == 1
    assert calls == [params['race_url']]


@pytest.mark.parametrize('scope,row_type,confirmed', [('gc', 'classification', False), ('result', 'one_day_result', True)])
def test_gc_and_one_day_storage(app, scope, row_type, confirmed):
    slug = 'pcs-' + uuid4().hex
    html = f'''<link rel="canonical" href="{BASE}{slug}-2026-{scope}">
        <h1>Example race <font>(1.2)</font></h1>
        <table class="results"><thead><tr><th data-code="rnk">Rnk</th>
        <th data-code="rider">Rider</th></tr></thead><tbody>
        <tr><td>1</td><td><a href="rider/{slug}">Example Rider</a></td></tr>
        </tbody></table>'''
    with app.app_context():
        try:
            namespace = Namespace(name=slug, slug=slug, database_name=slug, database_year=2026, gender='Men')
            db.session.add(namespace)
            db.session.flush()
            client = PcsClient(namespace)
            for _ in range(2):
                summary = client.sync_race_from_html(html=html, source_url=BASE + slug + '-2026-' + scope, commit=False)
            db.session.flush()
            assert summary['results'] == 1
            rider = PcsRider.query.filter_by(rider_slug=slug).one()
            result = PcsRiderResult.query.filter_by(rider_id=rider.id).one()
            assert result.row_type == row_type and result.confirmed_participation == confirmed
            assert result.stage_number is None
            assert PcsRaceResult.query.filter_by(namespace_id=namespace.id).count() == 1
        finally:
            db.session.rollback()


def test_upsert_scopes_and_rollback(app):
    with app.app_context():
        try:
            namespaces = [Namespace(name=label, slug=uuid4().hex, database_name='worlddb_test_' + uuid4().hex)
                          for label in ('PCS Alpha', 'PCS Beta')]
            db.session.add_all(namespaces)
            db.session.flush()
            payload = fixture_payload()
            # Synthetic slug prevents collision with other fixture tests.
            slug = 'parser-' + uuid4().hex
            payload['race_slug'] = slug
            payload['rows'] = deepcopy(payload['rows'][:2])
            for index, row in enumerate(payload['rows']):
                row['pcs_rider_path'] = 'rider/' + slug + '-' + str(index)
            params = dict(race_slug=slug, season_year=2026, gender='Men', race_url=BASE + slug + '/2026')
            first = store_race(params, payload, namespace_id=namespaces[0].id)
            db.session.flush()
            query = PcsRaceResult.query.filter_by(namespace_id=namespaces[0].id)
            ids = [row.id for row in query.order_by(PcsRaceResult.id)]
            assert len(ids) == 2
            payload['rows'][0]['position'] = 10
            payload['rows'][0]['result_position'] = '10'
            assert store_race(params, payload, namespace_id=namespaces[0].id)['pcs_race_id'] == first['pcs_race_id']
            db.session.flush()
            assert [row.id for row in query.order_by(PcsRaceResult.id)] == ids
            store_race(params, payload, namespace_id=namespaces[1].id)
            stage7 = dict(payload, scope='stage-7', stage_number=7)
            store_race(params, stage7, namespace_id=namespaces[0].id)
            db.session.flush()
            assert query.count() == 4
            assert PcsRaceResult.query.filter_by(namespace_id=namespaces[1].id).count() == 2
            rider = PcsRider.query.filter_by(rider_slug=slug + '-0').one()
            results = PcsRiderResult.query.filter_by(rider_id=rider.id).all()
            assert len(results) == 2
            assert {row.stage_number for row in results} == {6, 7}
            assert all(row.result_position == '10' and row.confirmed_participation for row in results)
            assert results[0].result_date == date(2026, 2, 21)
            status_payload = deepcopy(payload)
            status_payload['rows'][0].update(position=None, result_position='DNF')
            status_payload['rows'][1].update(position=None, result_position='DNS')
            store_race(params, status_payload, namespace_id=namespaces[0].id)
            store_race(params, status_payload, namespace_id=namespaces[0].id)
            db.session.flush()
            assert query.count() == 4
            db.session.expire_all()
            dnf = PcsRiderResult.query.filter_by(rider_id=rider.id, stage_number=6).one()
            assert dnf.result_position == 'DNF' and dnf.confirmed_participation
            dns_rider = PcsRider.query.filter_by(rider_slug=slug + '-1').one()
            dns = PcsRiderResult.query.filter_by(rider_id=dns_rider.id, stage_number=6).one()
            assert dns.result_position == 'DNS' and not dns.confirmed_participation
            bad = deepcopy(payload)
            bad['name'] = 'Must roll back'
            bad['rows'].append({})
            with pytest.raises(KeyError), db.session.begin_nested():
                store_race(params, bad, namespace_id=namespaces[0].id)
            db.session.expire_all()
            assert db.session.get(PcsRace, first['pcs_race_id']).name == 'UAE Tour'
            assert query.count() == 4
            with pytest.raises(ValueError), db.session.begin_nested():
                store_race(dict(params, gender='Women'), dict(payload, gender='Women'))
        finally:
            db.session.rollback()
