"""Discord review messages combine attachments and preserve every result scope."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.integrations import discord
from webapp.models import RaceResultMap


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('has_image', [False, True])
def test_submission_sends_one_message(app, monkeypatch, tmp_path, existing, has_image):
    image = tmp_path / 'approval.png'
    image.write_bytes(b'image')
    namespace = SimpleNamespace(id=1, slug='pcm2026', name='PCM 2026')
    settings = SimpleNamespace(id=1, channel_id='channel', approver_role_id='approvers')
    client = Mock(bot_token='synthetic-token')
    client.create_thread_from_message.return_value = {'id': 'starter-message'}
    client.send_message.return_value = {'id': 'starter-message'}
    client.send_message_with_file.return_value = {'id': 'starter-message'}
    monkeypatch.setattr(discord, 'DiscordClient', lambda: client)
    monkeypatch.setattr(discord, 'get_discord_settings_for_namespace_workflow', lambda *a: [settings])
    monkeypatch.setattr(discord, '_load_namespace_discord_scope_state', lambda *a, **kw: {'id': 1})
    monkeypatch.setattr(discord, '_build_stat_approval_image', lambda *a, **kw: str(image) if has_image else None)
    monkeypatch.setattr(discord, '_load_discord_workflow_thread', lambda *a, **kw: SimpleNamespace(thread_id='existing-thread') if existing else None)
    store = Mock()
    monkeypatch.setattr(discord, '_save_discord_workflow_thread', store)
    monkeypatch.setattr(discord, '_namespace_dashboard_url', lambda _: 'https://example.test/namespace')
    with app.app_context():
        assert discord.notify_discord_stat_scope_saved(
            namespace, 'Tour Test', 'https://example.test/review', 3,
            saved_by='editor', race_date=date(2026, 10, 30), first_cycling_race_id=42,
        )
    sender = client.send_message_with_file if has_image else client.send_message
    sender.assert_called_once()
    other = client.send_message if has_image else client.send_message_with_file
    other.assert_not_called()
    channel, message = sender.call_args.args[:2]
    assert channel == ('existing-thread' if existing else 'channel')
    assert 'Awaiting approval' in message
    if existing:
        client.create_thread_from_message.assert_not_called()
        store.assert_not_called()
    else:
        client.create_thread_from_message.assert_called_once_with('channel', 'starter-message', 'Stat Edits: Tour Test')
        client.create_thread.assert_not_called()
        store.assert_called_once()
        header = message.splitlines()[0]
        assert header.startswith('[pcm2026](')
        assert '): [' in header
        assert 'Tour Test' in header
        assert 'Namespace:' not in message
    if has_image:
        assert not image.exists()


def test_thread_is_created_from_the_submission_message(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    request = Mock(return_value={'id': 'starter-message'})
    monkeypatch.setattr(client, '_request', request)
    assert client.create_thread_from_message('channel', 'starter-message', 'Review') == {'id': 'starter-message'}
    request.assert_called_once_with(
        'POST', '/channels/channel/messages/starter-message/threads',
        {'name': 'Review', 'auto_archive_duration': 10080},
    )


def test_image_includes_overall_and_stage_edits(app, monkeypatch):
    schema = 'discord_' + uuid4().hex
    race_id = uuid4().int % 1000000000
    with app.app_context():
        mappings = [RaceResultMap(provider='firstcycling', external_race_id=str(race_id), external_stage_id=stage)
                    for stage in (None, '123456789')]
        db.session.add_all(mappings)
        db.session.commit()
        overall, stage = [row.race_result_id for row in mappings]
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        namespace = SimpleNamespace(build_connection_string=lambda: 'selected')
        monkeypatch.setattr(discord, 'create_engine', lambda url: engine if url == 'selected' else pytest.fail('Wrong namespace'))
        try:
            with engine.begin() as conn:
                columns = ', '.join(f'{column} integer' for column in discord.STAT_APPROVAL_IMAGE_COLUMNS.values())
                conn.execute(text(f'CREATE TABLE stat_change_snapshot (id serial, race_result_id integer, stat_list_id integer, cdb_cyclist_id integer, {columns})'))
                conn.execute(text('CREATE TABLE stat_changes (id serial, race_result_id integer, stat_list_id integer, cdb_cyclist_id integer, stat_name text, original_value integer, final_value integer, delta_value integer)'))
                conn.execute(text('CREATE TABLE cdb_cyclist (id integer, first_cycling_id integer, first_last_name text, first_name text, last_name text, team_id integer)'))
                conn.execute(text('CREATE TABLE cdb_team (id integer, team_name text, short_name text)'))
                conn.execute(text("INSERT INTO cdb_cyclist VALUES (1,NULL,'Rider A',NULL,NULL,NULL),(2,NULL,'Stage only',NULL,NULL,NULL)"))
                for result_id, rider_id, value in [(overall, 1, 72), (stage, 1, 73), (stage, 2, 75)]:
                    conn.execute(text('INSERT INTO stat_change_snapshot (race_result_id,cdb_cyclist_id) VALUES (:scope,:rider)'), {'scope': result_id, 'rider': rider_id})
                    conn.execute(text("INSERT INTO stat_changes (race_result_id,cdb_cyclist_id,stat_name,original_value,final_value,delta_value) VALUES (:scope,:rider,'mo',70,:value,:delta)"), {'scope': result_id, 'rider': rider_id, 'value': value, 'delta': value - 70})
                # An unrelated race with overlapping rider IDs must stay out of the image.
                conn.execute(text("INSERT INTO stat_changes (race_result_id,cdb_cyclist_id,stat_name,final_value) VALUES (-1,1,'mo',99)"))
                for result_id, stat_name, delta in [(overall, 'mm', -2), (stage, 'mm', 1), (overall, 'spr', 1), (stage, 'spr', -1)]:
                    conn.execute(text("INSERT INTO stat_changes (race_result_id,cdb_cyclist_id,stat_name,original_value,final_value,delta_value) VALUES (:scope,1,:stat,70,:final,:delta)"),
                                 {'scope': result_id, 'stat': stat_name, 'final': 70 + delta, 'delta': delta})
            assert set(discord._firstcycling_race_result_map_ids(race_id)) == {overall, stage}
            assert discord._firstcycling_race_result_map_ids(race_id, '123456789') == [stage]
            rows = discord._load_stat_approval_image_rows(namespace, first_cycling_race_id=race_id)
            assert len(rows) == 2
            assert {row['stats']['mo'] for row in rows} == {75}
            assert {row['cdb_cyclist_id']: row['changes']['mo']['delta_value'] for row in rows} == {1: 5, 2: 5}
            assert {row['rider_name'] for row in rows} == {'Rider A', 'Stage only'}
            rider = next(row for row in rows if row['cdb_cyclist_id'] == 1)
            assert rider['changes']['mm']['delta_value'] == -1
            assert rider['stats']['mm'] == 69
            assert rider['changes']['spr']['delta_value'] == 0
            assert rider['stats']['spr'] == 70
        finally:
            engine.dispose()
            for mapping in mappings:
                db.session.delete(mapping)
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()

