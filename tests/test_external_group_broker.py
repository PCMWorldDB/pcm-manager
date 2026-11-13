"""Optional real-Redis priority check; no external provider requests are made."""
import os
from threading import Event
from urllib.parse import urlsplit

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker


def test_high_group_waits_for_active_group_then_precedes_low_backlog(app):
    url = os.environ.get('WORLDDB_TEST_REDIS_URL')
    if not url:
        pytest.skip('Set WORLDDB_TEST_REDIS_URL to the disposable worlddb-agent-cache container')
    parsed = urlsplit(url)
    if parsed.scheme != 'redis' or parsed.hostname != 'worlddb-agent-cache':
        pytest.fail('Broker tests require the disposable worlddb-agent-cache Redis container')

    from webapp.extensions import celery

    broker = Celery('group_priority_test', broker=url, set_as_current=False)
    broker.conf.update(
        task_queues=celery.conf.task_queues,
        broker_transport_options=celery.conf.broker_transport_options,
        worker_concurrency=celery.conf.worker_concurrency,
        worker_prefetch_multiplier=celery.conf.worker_prefetch_multiplier,
    )
    started, release, finished = Event(), Event(), Event()
    seen = []

    @broker.task(name='test.record_group', acks_late=True)
    def record_group(label):
        if label == 'active-low':
            started.set()
            if not release.wait(10):
                raise RuntimeError('Test did not release the active group')
        seen.append(label)
        if len(seen) == 4:
            finished.set()

    try:
        with start_worker(broker, pool='threads', concurrency=1, perform_ping_check=False):
            try:
                record_group.apply_async(args=['active-low'], queue='external.low')
                assert started.wait(10)
                record_group.apply_async(args=['waiting-low-1'], queue='external.low')
                record_group.apply_async(args=['waiting-low-2'], queue='external.low')
                record_group.apply_async(args=['manual-high'], queue='external.high')
            finally:
                release.set()
            assert finished.wait(10)
        assert seen == ['active-low', 'manual-high', 'waiting-low-1', 'waiting-low-2']
    finally:
        broker.close()
        celery.set_current()
        celery.set_default()
