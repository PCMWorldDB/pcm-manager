"""Regression coverage for shared queue state and provider isolation."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from webapp.extensions import db
from webapp.models import ExternalCallTask, ExternalCallTaskGroup, ExternalSyncAPICallLog, ExternalSyncLog
from webapp import worker as queue
from webapp.worker import RetryableFetch, validate_request


def request(provider='pcs', slug='test-rider'):
    return {'provider': provider, 'endpoint': 'get_rider_profile',
            'params': {'rider_slug': slug} if provider == 'pcs' else {'rider_id': 123}}


@pytest.fixture
def queue_context(app):
    with app.app_context():
        yield
        db.session.rollback()
        db.session.query(ExternalCallTask).delete()
        db.session.query(ExternalCallTaskGroup).delete()
        db.session.commit()


def tasks(group):
    return db.session.execute(select(ExternalCallTask).where(
        ExternalCallTask.task_group_id == group.id).order_by(ExternalCallTask.id)).scalars().all()


def test_mixed_group_and_duplicate_delivery(queue_context, monkeypatch):
    calls = []
    monkeypatch.setattr(queue, 'fetch_profile', lambda provider, params: calls.append(provider) or {'name': 'Rider'})
    group = queue.enqueue_group([request(), request('firstcycling')])
    db.session.commit()
    pcs, fc = tasks(group)
    assert pcs.related_to_id is None and pcs.related_to_table is None
    assert fc.related_to_id is None and fc.related_to_table is None
    assert group.provider is None
    assert queue.execute_task(pcs.id)
    assert not queue.execute_task(pcs.id)
    assert queue.execute_task(fc.id)
    db.session.refresh(group)
    assert calls == ['pcs', 'firstcycling']
    assert group.status == 'success' and group.synced_count == 2
    assert json.loads(pcs.result_json) == {'name': 'Rider'}


def test_pcs_global_cooldown_does_not_block_fc(queue_context, monkeypatch):
    def fetch(provider, params):
        if provider == 'pcs':
            raise RetryableFetch('rate limited', delay=120, http_status=429)
        return {'ok': True}
    monkeypatch.setattr(queue, 'fetch_profile', fetch)
    first = queue.enqueue_group([request()])
    second = queue.enqueue_group([request(slug='other-rider'), request('firstcycling')])
    db.session.commit()
    pcs = tasks(first)[0]
    other, fc = tasks(second)
    assert queue.execute_task(pcs.id)
    assert pcs.status == 'retry_wait' and pcs.http_status == 429
    assert not queue.execute_task(pcs.id)
    assert not queue.execute_task(other.id)
    assert queue.execute_task(fc.id)
    pcs.available_at = datetime.utcnow() - timedelta(seconds=1)
    pcs.started_at = datetime.utcnow() - timedelta(minutes=5)
    pcs.attempt = pcs.max_attempts - 1
    db.session.commit()
    assert queue.execute_task(pcs.id)
    assert pcs.status == 'failed'
    assert not queue.execute_task(other.id)  # final attempt still imposes a cooldown
    assert db.session.get(ExternalCallTaskGroup, first.id).status == 'failed'


def test_pcs_spacing_and_running_gate(queue_context, monkeypatch):
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: {'ok': True})
    group = queue.enqueue_group([request(), request(slug='other-rider')])
    db.session.commit()
    first, second = tasks(group)
    assert queue.execute_task(first.id)
    assert not queue.execute_task(second.id)
    first.started_at = datetime.utcnow() - timedelta(minutes=1)
    first.status = 'running'
    db.session.commit()
    assert not queue.execute_task(second.id)
    first.status = 'success'
    db.session.commit()
    assert queue.execute_task(second.id)


def test_partial_group_and_historical_aliases(queue_context, monkeypatch):
    group = queue.enqueue_group([request('firstcycling'), request()])
    historical = ExternalSyncAPICallLog(provider='firstcycling', endpoint='old', request_url='redacted', outcome='success', external_sync_log=group)
    db.session.add(historical)
    db.session.commit()
    assert ExternalSyncLog is ExternalCallTaskGroup
    assert historical.external_sync_log_id == group.id
    assert not queue.execute_task(historical.id)
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: {'ok': True})
    first, second = [task for task in tasks(group) if task.status != 'historical']
    assert queue.execute_task(first.id)
    def fail(*args):
        raise ValueError('invalid profile')
    monkeypatch.setattr(queue, 'fetch_profile', fail)
    assert queue.execute_task(second.id)
    db.session.refresh(group)
    assert group.status == 'partial' and group.failed_count == 1


def test_validate_before_any_insert(queue_context):
    with pytest.raises(ValueError):
        queue.enqueue_group([request(), request(slug='../../etc/passwd')])
    assert db.session.query(ExternalCallTaskGroup).count() == 0
    with pytest.raises(ValueError):
        validate_request('pcs', 'arbitrary_url', {'url': 'http://localhost'})
    with pytest.raises(ValueError):
        validate_request('unknown', 'get_rider_profile', {})


def test_optional_namespace_context(queue_context, monkeypatch):
    from uuid import uuid4
    from webapp.models import Namespace
    token = uuid4().hex
    first = Namespace(name=token, slug=token, database_name='never_connect_a')
    second = Namespace(name=token + 'b', slug=token + 'b', database_name='never_connect_b')
    db.session.add_all([first, second])
    db.session.flush()
    associated = {**request(), 'related_to_table': 'rider', 'related_to_id': 1}
    a = queue.enqueue_group([associated], namespace_id=first.id)
    b = queue.enqueue_group([associated], namespace_id=second.id)
    db.session.commit()
    assert a.namespace.id == first.id and b.namespace.id == second.id
    assert tasks(a)[0].task_group_id != tasks(b)[0].task_group_id
    a_task, b_task = tasks(a)[0], tasks(b)[0]
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: {'ok': True})
    assert queue.execute_task(a_task.id)
    # Allow the other Namespace's PCS request after the shared spacing window.
    a_task.started_at = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    def fail(*args):
        raise ValueError('profile missing')
    monkeypatch.setattr(queue, 'fetch_profile', fail)
    assert queue.execute_task(b_task.id)
    db.session.expire_all()
    for namespace, expected_task, expected_status in (
        (first, a_task, 'success'), (second, b_task, 'failed'),
    ):
        history = db.session.execute(select(ExternalCallTask).join(
            ExternalCallTaskGroup, ExternalCallTask.task_group_id == ExternalCallTaskGroup.id
        ).where(
            ExternalCallTaskGroup.namespace_id == namespace.id,
            ExternalCallTask.related_to_table == 'rider', ExternalCallTask.related_to_id == 1,
        )).scalars().all()
        assert [task.id for task in history] == [expected_task.id]
        assert history[0].status == expected_status
    # Database delete clears context while retaining the queued requests.
    db.session.query(Namespace).filter_by(id=first.id).delete()
    db.session.commit()
    db.session.expire_all()
    assert a.namespace is None and a.namespace_id is None
    assert b.namespace.id == second.id
    assert len(tasks(a)) == 1
    assert tasks(a)[0].related_to_id == 1
    assert tasks(a)[0].related_to_table == 'rider'
    db.session.query(Namespace).filter_by(id=second.id).delete()
    db.session.commit()


def test_locked_task_is_not_claimed_twice(queue_context, monkeypatch):
    from sqlalchemy.orm import Session
    group = queue.enqueue_group([request('firstcycling')])
    db.session.commit()
    task = tasks(group)[0]
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: pytest.fail('must not fetch locked task'))
    with Session(db.engines['main']) as other:
        other.execute(select(ExternalCallTask).where(ExternalCallTask.id == task.id).with_for_update()).scalar_one()
        assert not queue.execute_task(task.id)
        other.rollback()


def test_group_finishes_before_new_high_priority_work(queue_context, monkeypatch):
    low = queue.enqueue_group([request('firstcycling'), request('firstcycling')], origin='scheduled')
    db.session.commit()
    low_ids = [task.id for task in tasks(low)]
    seen = []
    high_groups = []
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: None)
    assert queue.submit_external_group(low.id)
    token = low.worker_message_id

    def fetch(*args):
        if not seen:
            for _ in range(2):
                group = queue.create_manual_external_group([request('firstcycling')])
                high_groups.append(group)
            assert low.status == 'running' and not low.is_worker_queued
        seen.append(True)
        return {'ok': True}

    executed = []
    original = queue.execute_task

    def execute(task_id):
        executed.append(task_id)
        return original(task_id)

    monkeypatch.setattr(queue, 'fetch_profile', fetch)
    monkeypatch.setattr(queue, 'execute_task', execute)
    assert queue.run_external_group.run(low.id, token) == 2
    assert executed == low_ids
    assert all(group.status == 'queued' and group.is_worker_queued for group in high_groups)
    assert queue.run_external_group.run(low.id, token) == 0  # duplicate delivery
    assert queue.run_external_group.run(low.id, 'stale-token') == 0
    assert low.priority == 'low' and low.origin == 'scheduled'


def test_group_keeps_ownership_during_cooldown_and_retry(queue_context, monkeypatch):
    high = queue.enqueue_group([request(), request('firstcycling')])
    low = queue.enqueue_group([request('firstcycling')], origin='scheduled')
    db.session.commit()
    pcs, future = tasks(high)
    low_task_id = tasks(low)[0].id
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: None)
    queue.submit_external_group(high.id)
    queue.submit_external_group(low.id)
    calls = []

    def fetch(provider, params):
        calls.append(provider)
        if provider == 'pcs' and calls.count('pcs') == 1:
            raise RetryableFetch('cooldown', delay=120, http_status=429)
        return {'ok': True}

    monkeypatch.setattr(queue, 'fetch_profile', fetch)
    waits = []

    def advance_clock(seconds):
        waits.append(seconds)
        assert high.status == 'running' and low.status == 'queued'
        assert db.session.get(ExternalCallTask, low_task_id).status == 'queued'
        pcs.available_at = datetime.utcnow() - timedelta(seconds=1)
        pcs.started_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()

    monkeypatch.setattr(queue.time, 'sleep', advance_clock)
    assert queue.run_external_group.run(high.id, high.worker_message_id) == 3
    assert calls == ['pcs', 'firstcycling', 'pcs']
    assert waits and high.status == 'success'
    assert future.status == 'success'
    assert low.status == 'queued'


def test_dispatch_lock_excludes_second_runner(queue_context, monkeypatch):
    from celery.exceptions import Retry
    monkeypatch.setattr(queue, '_next_group_task', lambda *args: pytest.fail('must not dispatch'))
    with db.engines['main'].connect() as connection:
        from sqlalchemy import text
        connection.execute(text('SELECT pg_advisory_lock(812734903)'))
        connection.commit()
        try:
            # apply() supplies a task request so retry raises Celery's Retry.
            with pytest.raises(Retry):
                queue.run_external_group.apply(args=(1, 'unused'), throw=True)
        finally:
            connection.execute(text('SELECT pg_advisory_unlock(812734903)'))
            connection.commit()


@pytest.mark.parametrize('kwargs', [{'origin': 'unknown'}, {'priority': 'urgent'}])
def test_invalid_group_policy_before_insert(queue_context, kwargs):
    with pytest.raises(ValueError):
        queue.enqueue_group([request()], **kwargs)
    assert db.session.query(ExternalCallTaskGroup).count() == 0


def test_scheduled_batching_unlimited_database_backlog_and_queue_cap(queue_context, monkeypatch):
    monkeypatch.setitem(queue.current_app.config, 'PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE', 2)
    monkeypatch.setitem(queue.current_app.config, 'PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED', 2)
    groups = queue.enqueue_scheduled_pcs_groups([request(slug=f'rider-{i}') for i in range(9)])
    db.session.commit()
    assert [len(tasks(group)) for group in groups] == [2, 2, 2, 2, 1]
    assert all(group.priority == 'low' and not group.is_worker_queued for group in groups)
    deliveries = []
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: deliveries.append(kwargs))
    ids = [group.id for group in groups]
    assert queue.poll_external_calls.run() == ids[:2]
    assert [delivery['args'][0] for delivery in deliveries] == ids[:2]
    assert all(delivery['queue'] == 'external.low' for delivery in deliveries)
    assert queue.poll_external_calls.run() == []
    # A running group no longer occupies a waiting slot.
    groups[0].status = 'running'
    groups[0].is_worker_queued = False
    db.session.commit()
    assert queue.poll_external_calls.run() == ids[2:3]
    # Manual work commits and publishes immediately despite a full low queue.
    manual = queue.create_manual_external_group([request('firstcycling')])
    assert manual.is_worker_queued and manual.status == 'queued'
    assert deliveries[-1]['queue'] == 'external.high'
    assert not queue.submit_external_group(manual.id)


def test_manual_publish_failure_retains_group_for_hourly_recovery(queue_context, monkeypatch):
    def fail(**kwargs):
        raise ConnectionError('synthetic broker failure')

    monkeypatch.setattr(queue.run_external_group, 'apply_async', fail)
    group = queue.create_manual_external_group([request('firstcycling')])
    assert group.status == 'queued' and not group.is_worker_queued
    assert group.worker_message_id is None
    deliveries = []
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: deliveries.append(kwargs))
    assert queue.poll_external_calls.run() == [group.id]
    assert group.is_worker_queued
    assert queue.poll_external_calls.run() == []
    assert len(deliveries) == 1


def test_failed_commit_never_publishes_manual_group(queue_context, monkeypatch):
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: pytest.fail('must not publish'))

    def fail():
        raise RuntimeError('synthetic commit failure')

    monkeypatch.setattr(db.session, 'commit', fail)
    with pytest.raises(RuntimeError, match='commit failure'):
        queue.create_manual_external_group([request()])
    db.session.rollback()
    assert db.session.query(ExternalCallTaskGroup).count() == 0


def test_interrupted_group_preserves_failed_request_and_finishes_rest(queue_context, monkeypatch):
    group = queue.enqueue_group([request('firstcycling'), request('firstcycling')])
    group.status = 'running'
    group.worker_message_id = 'synthetic-redelivery'
    db.session.commit()
    interrupted, pending = tasks(group)
    interrupted.status = 'running'
    interrupted.started_at = datetime.utcnow()
    db.session.commit()
    calls = []
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: calls.append(True) or {'ok': True})
    assert queue.run_external_group.run(group.id, group.worker_message_id) == 1
    assert interrupted.status == 'failed' and 'interrupted' in interrupted.error_message
    assert pending.status == 'success' and group.status == 'partial'
    assert len(calls) == 1


def test_invalid_scheduled_selection_before_any_group_insert(queue_context):
    for selection in ([request(), request('firstcycling')], [request(), request()]):
        with pytest.raises(ValueError):
            queue.enqueue_scheduled_pcs_groups(selection)
        assert db.session.query(ExternalCallTaskGroup).count() == 0


def test_orphaned_pcs_request_cannot_block_another_group(queue_context, monkeypatch):
    orphan = queue.enqueue_group([request()])
    active = queue.enqueue_group([request(slug='active-rider')])
    active.worker_message_id = 'active-message'
    db.session.commit()
    interrupted = tasks(orphan)[0]
    interrupted.status = 'running'
    interrupted.started_at = datetime.utcnow() - timedelta(minutes=1)
    orphan.status = 'running'
    db.session.commit()
    monkeypatch.setattr(queue, 'fetch_profile', lambda *args: {'ok': True})
    assert queue.run_external_group.run(active.id, active.worker_message_id) == 1
    assert interrupted.status == 'failed' and orphan.status == 'failed'
    assert active.status == 'success'


def test_concurrent_publishers_share_low_queue_cap(app, queue_context, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setitem(app.config, 'PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED', 1)
    groups = [queue.enqueue_group([request()], origin='scheduled') for _ in range(2)]
    db.session.commit()
    ids = [group.id for group in groups]
    deliveries = []
    monkeypatch.setattr(queue.run_external_group, 'apply_async', lambda **kwargs: deliveries.append(kwargs))
    barrier = Barrier(2)

    def submit(group_id):
        with app.app_context():
            barrier.wait(timeout=5)
            return queue.submit_external_group(group_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ids))
    assert sorted(results) == [False, True]
    assert len(deliveries) == 1
    db.session.expire_all()
    assert sum(group.is_worker_queued for group in groups) == 1
