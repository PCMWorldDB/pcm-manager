"""Approvals mark the starter message without disrupting replies or archiving."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from webapp.integrations import discord


@pytest.mark.parametrize('method,http_method', [('add_reaction', 'PUT'), ('remove_reaction', 'DELETE')])
def test_reaction_api_encodes_checkbox(monkeypatch, method, http_method):
    client = discord.DiscordClient(bot_token='synthetic-token')
    request = Mock(return_value={})
    monkeypatch.setattr(client, '_request', request)
    getattr(client, method)('channel', 'starter', '\u2705')
    request.assert_called_once_with(http_method, '/channels/channel/messages/starter/reactions/%E2%9C%85/@me')


@pytest.mark.parametrize('scope', ['race', 'list'])
@pytest.mark.parametrize('reaction_fails', [False, True])
def test_approval_reacts_and_preserves_reply_and_archive(monkeypatch, scope, reaction_fails):
    app = Flask(__name__)
    namespace = SimpleNamespace(id=1)
    settings = SimpleNamespace(id=2, channel_id='parent')
    client = Mock(bot_token='synthetic-token')
    client.submission_message_reference.return_value = ('parent', 'starter')
    if reaction_fails:
        client.add_reaction.side_effect = discord.DiscordIntegrationError('synthetic failure')
    monkeypatch.setattr(discord, 'DiscordClient', lambda: client)
    monkeypatch.setattr(discord, 'get_discord_settings_for_namespace_workflow', lambda *a: [settings])
    lookup = Mock(return_value=SimpleNamespace(thread_id='starter'))
    monkeypatch.setattr(discord, '_load_discord_workflow_thread', lookup)
    with app.app_context():
        if scope == 'race':
            assert discord.notify_discord_race_stat_approved(namespace, 42, None, approved_by='approver')
            assert lookup.call_args.kwargs['first_cycling_race_id'] == 42
        else:
            assert discord.notify_discord_stat_list_approved(namespace, 42, approved_by='approver')
            assert lookup.call_args.kwargs['stat_list_id'] == 42
    client.send_message.assert_called_once_with('starter', '\u2705 Approved by approver')
    client.add_reaction.assert_called_once_with('parent', 'starter', '\u2705')
    client.archive_thread.assert_called_once_with('starter')


def test_missing_thread_is_not_reacted_to(monkeypatch):
    app = Flask(__name__)
    client = Mock(bot_token='synthetic-token')
    client.submission_message_reference.return_value = ('parent', 'starter')
    monkeypatch.setattr(discord, 'DiscordClient', lambda: client)
    monkeypatch.setattr(discord, 'get_discord_settings_for_namespace_workflow', lambda *a: [SimpleNamespace(id=1)])
    monkeypatch.setattr(discord, '_load_discord_workflow_thread', lambda *a, **kw: None)
    with app.app_context():
        assert not discord.notify_discord_stat_list_approved(SimpleNamespace(id=1), 42)
    client.send_message.assert_not_called()
    client.add_reaction.assert_not_called()
    client.archive_thread.assert_not_called()


def test_reaction_targets_thread_created_notice(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    request = Mock(return_value=[
        {'id': 'unrelated', 'type': 18, 'message_reference': {'channel_id': 'other-thread'}},
        {'id': 'notice', 'type': 18, 'message_reference': {'channel_id': 'thread'}},
    ])
    monkeypatch.setattr(client, '_request', request)
    assert client.submission_message_reference('parent', 'thread') == ('parent', 'notice')
    request.assert_called_once_with('GET', '/channels/parent/messages?around=thread&limit=100')


def test_legacy_parent_starter_is_preserved(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    monkeypatch.setattr(client, '_request', Mock(side_effect=[[], [{'id': 'reference', 'type': 21}]]))
    assert client.submission_message_reference('parent', 'thread') == ('parent', 'thread')


def test_missing_notice_does_not_target_thread_details(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    monkeypatch.setattr(client, '_request', Mock(side_effect=[[], [{'id': 'details', 'type': 0}]]))
    with pytest.raises(discord.DiscordIntegrationError, match='thread-created notice'):
        client.submission_message_reference('parent', 'thread')

def test_silent_message_sets_suppress_notifications(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    request = Mock(return_value={})
    monkeypatch.setattr(client, '_request', request)
    client.send_message('thread', 'Submission details', silent=True)
    assert request.call_args.args[2]['flags'] == 4096
    client.send_message('thread', 'Later submission')
    assert 'flags' not in request.call_args.args[2]


def test_silent_attachment_sets_suppress_notifications(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    image = tmp_path / 'approval.png'
    image.write_bytes(b'synthetic-image')
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{}'
    upload = Mock(return_value=response)
    monkeypatch.setattr(discord.urllib.request, 'urlopen', upload)
    client = discord.DiscordClient(bot_token='synthetic-token')
    client.send_message_with_file('thread', 'Submission details', str(image), silent=True)
    body = upload.call_args.args[0].data
    assert b'"flags": 4096' in body
    assert b'synthetic-image' in body
