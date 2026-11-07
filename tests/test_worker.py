"""Check worker loading in a fresh process, as it is loaded by the Celery CLI."""
import subprocess
import sys
from pathlib import Path


def test_worker_entrypoint_and_task_context():
    script = '''
from types import SimpleNamespace
from unittest.mock import Mock

from celery.app.utils import find_app
from flask import Flask, current_app

import webapp
from webapp.extensions import init_extensions
from webapp.config import DevelopmentConfig

def create_app():
    app = Flask(__name__)
    app.config.from_object(DevelopmentConfig)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_BINDS'] = {'main': 'sqlite://'}
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
    init_extensions(app)
    return app

webapp.create_app = Mock(side_effect=create_app)
from webapp import worker
webapp.create_app.assert_not_called()

celery = find_app('webapp.worker:celery')
assert find_app('webapp.worker:celery') is celery
webapp.create_app.assert_called_once_with()
assert worker.run_external_sync_task.name == 'webapp.tasks.run_external_sync'
assert worker.run_activity_monitor_run_task.name == 'webapp.tasks.run_activity_monitor_run'
assert worker.run_external_call.name == 'webapp.tasks.run_external_call'
assert worker.run_external_group.name == 'webapp.tasks.run_external_group'
assert worker.poll_external_calls.name == 'webapp.tasks.poll_external_calls'
assert celery.conf.beat_schedule['poll-external-calls']['task'] == worker.poll_external_calls.name

assert celery.conf.worker_concurrency == 1
assert celery.conf.worker_prefetch_multiplier == 1
assert worker.run_external_group.acks_late
assert worker.run_external_group.reject_on_worker_lost
assert [queue.name for queue in celery.conf.task_queues] == ['celery', 'external.high', 'external.low']
assert celery.conf.broker_transport_options['queue_order_strategy'] == 'sorted'
assert celery.conf.beat_schedule['poll-external-calls']['schedule'] == 3600

def dispatch():
    assert current_app.name == __name__
    return [7]

worker.dispatch_queued_external_groups = dispatch
assert worker.poll_external_calls.apply(throw=True).result == [7]
assert worker.run_external_call.apply(args=(7,), throw=True).result is False

def sync_race_calendar(**kwargs):
    assert current_app.name == __name__
    return {'requested': 1, 'synced': 1, 'skipped': 0}

worker.FirstCyclingClient = Mock(return_value=SimpleNamespace(
    sync_race_calendar=sync_race_calendar,
))
worker._update_sync_log = Mock()
worker.run_external_sync_task.apply(
    args=(1, worker.SYNC_TYPE_RACE, 2026, 'men'), throw=True,
)
assert worker._update_sync_log.call_args.kwargs['status'] == 'success'
'''
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
