"""Synthetic HTTP fixtures for the independent external call providers."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest

from webapp import worker as providers


def test_pcs_profile_fetch(monkeypatch):
    html = b'''<h1>Test Rider</h1><link rel="canonical" href="https://www.procyclingstats.com/rider/test-rider">
    <table class="rdrResults"><tr><th>Date</th><th>Result</th><th>Race</th></tr>
    <tr><td>12.04</td><td>DNF</td><td><a href="/race/test-race-2025">Test Race</a></td></tr></table>'''
    class Opener:
        def open(self, req, timeout):
            assert req.full_url == 'https://www.procyclingstats.com/rider/test-rider/road'
            assert timeout == 30
            return BytesIO(html)
    monkeypatch.setattr(providers, 'build_opener', lambda *args: Opener())
    result = providers.fetch_profile('pcs', {'rider_slug': 'test-rider'})
    assert result['profile']['canonical_slug'] == 'test-rider'
    assert result['rows'][0]['result'] == 'DNF'
    assert result['rows'][0]['year'] == 2025


@pytest.mark.parametrize('code', [403, 429, 503])
def test_pcs_retry_after(monkeypatch, code):
    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, code, 'busy', {'Retry-After': '180'}, None)
    monkeypatch.setattr(providers, 'build_opener', lambda *args: Opener())
    with pytest.raises(providers.RetryableFetch) as error:
        providers.fetch_profile('pcs', {'rider_slug': 'test-rider'})
    assert error.value.delay == 180 and error.value.http_status == code
    future = datetime.now(timezone.utc) + timedelta(seconds=180)
    assert 175 <= providers.retry_after(format_datetime(future)) <= 180


def test_fc_uses_no_pcs_delay_or_internal_retries(monkeypatch):
    from webapp.integrations.firstcycling.api import FirstCyclingClient
    class Client:
        def __init__(self):
            self.request_delay_seconds = 99
            self.retry_attempts = 99
        def get_rider(self, rider_id):
            assert rider_id == 7
            assert self.request_delay_seconds == self.retry_attempts == 0
            return {'name': 'FC Rider'}, 'redacted'
    monkeypatch.setattr('webapp.integrations.firstcycling.api.FirstCyclingClient', Client)
    assert providers.fetch_profile('firstcycling', {'rider_id': 7}) == {'name': 'FC Rider'}
    assert FirstCyclingClient is not Client
