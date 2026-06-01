"""Discord integration scaffold."""

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid

from flask import current_app, has_app_context, has_request_context, url_for
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import DiscordSettings, DiscordWorkflowThread, RaceResultMap

logger = logging.getLogger('discord')


class DiscordIntegrationError(Exception):
    """Raised when Discord integration configuration or execution fails."""


class DiscordClient:
    """Small Discord bot API client for outbound notifications."""

    api_base_url = 'https://discord.com/api/v10'

    def __init__(self, webhook_url=None, bot_token=None):
        if has_app_context():
            configured_webhook_url = current_app.config.get('DISCORD_WEBHOOK_URL')
            configured_bot_token = current_app.config.get('DISCORD_BOT_TOKEN')
        else:
            configured_webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
            configured_bot_token = os.getenv('DISCORD_BOT_TOKEN')

        self.webhook_url = webhook_url or configured_webhook_url
        self.bot_token = bot_token or configured_bot_token

    def is_configured(self):
        """Return True when at least one Discord auth mechanism is configured."""
        return bool(self.webhook_url or self.bot_token)

    def _request(self, method, path, payload=None):
        if not self.bot_token:
            raise DiscordIntegrationError('Discord bot token is not configured.')

        body = None
        headers = {
            'Authorization': f'Bot {self.bot_token}',
            'Content-Type': 'application/json',
            'User-Agent': 'WorldDB Discord Integration',
        }
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')

        request = urllib.request.Request(
            f'{self.api_base_url}{path}',
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read().decode('utf-8')
                logger.info('Discord API message sent: %s %s -> %s', method, path, response.status)
        except urllib.error.HTTPError as err:
            details = err.read().decode('utf-8', errors='replace')
            logger.error('Discord API error: %s %s -> %s %s', method, path, err.code, details)
            raise DiscordIntegrationError(f'Discord API error {err.code}: {details}') from err
        except urllib.error.URLError as err:
            logger.error('Discord API connection failed: %s %s -> %s', method, path, err.reason)
            raise DiscordIntegrationError(f'Discord API connection failed: {err.reason}') from err

        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except ValueError as err:
            raise DiscordIntegrationError('Discord API returned invalid JSON.') from err

    def create_thread(self, channel_id, thread_name):
        """Create a public thread in a channel."""
        if not channel_id:
            raise DiscordIntegrationError('Discord channel ID is not configured.')

        return self._request(
            'POST',
            f'/channels/{channel_id}/threads',
            {
                'name': thread_name[:100],
                'type': 11,
                'auto_archive_duration': 10080,
            },
        )

    def send_message(self, channel_reference, content):
        """Send a message to a Discord channel or thread."""
        if not channel_reference:
            raise DiscordIntegrationError('Discord channel or thread ID is required.')

        return self._request(
            'POST',
            f'/channels/{channel_reference}/messages',
            {
                'content': content,
                'allowed_mentions': {'parse': ['roles']},
            },
        )

    def send_message_with_file(self, channel_reference, content, file_path, file_name=None):
        """Send a message with one file attachment to a Discord channel."""
        if not channel_reference:
            raise DiscordIntegrationError('Discord channel ID is required.')
        if not file_path or not os.path.exists(file_path):
            raise DiscordIntegrationError('Discord attachment file could not be found.')
        if not self.bot_token:
            raise DiscordIntegrationError('Discord bot token is not configured.')

        upload_name = file_name or os.path.basename(file_path)
        mime_type = mimetypes.guess_type(upload_name)[0] or 'application/octet-stream'
        payload = json.dumps({
            'content': content,
            'allowed_mentions': {'parse': ['roles']},
        }).encode('utf-8')
        with open(file_path, 'rb') as handle:
            file_bytes = handle.read()

        boundary = f'----WorldDBDiscord{uuid.uuid4().hex}'
        body_parts = [
            f'--{boundary}\r\n'.encode('utf-8'),
            b'Content-Disposition: form-data; name="payload_json"\r\n',
            b'Content-Type: application/json\r\n\r\n',
            payload,
            b'\r\n',
            f'--{boundary}\r\n'.encode('utf-8'),
            f'Content-Disposition: form-data; name="files[0]"; filename="{upload_name}"\r\n'.encode('utf-8'),
            f'Content-Type: {mime_type}\r\n\r\n'.encode('utf-8'),
            file_bytes,
            b'\r\n',
            f'--{boundary}--\r\n'.encode('utf-8'),
        ]
        request = urllib.request.Request(
            f'{self.api_base_url}/channels/{channel_reference}/messages',
            data=b''.join(body_parts),
            headers={
                'Authorization': f'Bot {self.bot_token}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'User-Agent': 'WorldDB Discord Integration',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response_body = response.read().decode('utf-8')
                logger.info('Discord API file message sent: %s -> %s', channel_reference, response.status)
        except urllib.error.HTTPError as err:
            details = err.read().decode('utf-8', errors='replace')
            logger.error('Discord API file upload error: %s -> %s %s', channel_reference, err.code, details)
            raise DiscordIntegrationError(f'Discord API error {err.code}: {details}') from err
        except urllib.error.URLError as err:
            logger.error('Discord API file upload connection failed: %s -> %s', channel_reference, err.reason)
            raise DiscordIntegrationError(f'Discord API connection failed: {err.reason}') from err

        return json.loads(response_body) if response_body else {}

    def archive_thread(self, thread_id):
        """Archive a Discord thread."""
        if not thread_id:
            raise DiscordIntegrationError('Discord thread ID is required.')

        return self._request(
            'PATCH',
            f'/channels/{thread_id}',
            {'archived': True},
        )


def get_discord_settings_for_namespace(namespace):
    if namespace is None:
        return None

    return DiscordSettings.query.filter_by(namespace_id=namespace.id).first()


def get_discord_settings_for_namespace_workflow(namespace, workflow):
    if namespace is None:
        return []

    return DiscordSettings.query.filter_by(
        namespace_id=namespace.id,
        workflow=workflow,
        is_enabled=True,
    ).order_by(DiscordSettings.id.asc()).all()


def _firstcycling_race_result_map_ids(first_cycling_race_id=None, first_cycling_stage_id=None):
    if first_cycling_race_id is None:
        return []

    query = RaceResultMap.query.filter_by(
        provider='firstcycling',
        external_race_id=str(first_cycling_race_id),
    )
    if first_cycling_stage_id is None:
        query = query.filter(RaceResultMap.external_stage_id.is_(None))
    else:
        query = query.filter(RaceResultMap.external_stage_id == str(first_cycling_stage_id))

    return [row.race_result_id for row in query.all()]


def _discord_snapshot_scope_where_clause(race_result_ids=None, stat_list_id=None):
    if stat_list_id is not None:
        return (
            """
            race_result_id IS NULL
            AND stat_list_id = :list_id
            """,
            {'list_id': stat_list_id},
        )

    normalized_race_result_ids = [
        race_result_id
        for race_result_id in (race_result_ids or [])
        if race_result_id is not None
    ]
    if not normalized_race_result_ids:
        return None, {}

    return (
        """
        race_result_id = ANY(:race_result_ids)
        AND stat_list_id IS NULL
        """,
        {'race_result_ids': normalized_race_result_ids},
    )


def _load_namespace_discord_scope_state(
    namespace,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    if namespace is None:
        return None

    race_result_ids = []
    if stat_list_id is None:
        race_result_ids = _firstcycling_race_result_map_ids(
            first_cycling_race_id=first_cycling_race_id,
            first_cycling_stage_id=first_cycling_stage_id,
        )
        if not race_result_ids:
            return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            columns = {
                column_name
                for column_name in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'stat_change_snapshot'
                        """
                    )
                ).scalars()
            }
            if 'id' not in columns or 'race_result_id' not in columns or 'stat_list_id' not in columns:
                return None

            selected_columns = ['id']
            for column_name in ('discord_thread_id', 'discord_thread_url', 'discord_notified_at'):
                if column_name in columns:
                    selected_columns.append(column_name)

            where_clause, params = _discord_snapshot_scope_where_clause(
                race_result_ids=race_result_ids,
                stat_list_id=stat_list_id,
            )
            if not where_clause:
                return None
            row = conn.execute(
                text(
                    f"""
                    SELECT {', '.join(selected_columns)}
                    FROM stat_change_snapshot
                    WHERE {where_clause}
                    ORDER BY id ASC
                    LIMIT 1
                    """
                ),
                params,
            ).mappings().first()
    finally:
        engine.dispose()

    if row is None:
        return None

    row_dict = dict(row)
    row_dict.setdefault('discord_thread_id', None)
    row_dict.setdefault('discord_thread_url', None)
    return row_dict


def _update_namespace_discord_scope_thread(
    namespace,
    thread_id,
    thread_url=None,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    if namespace is None or not thread_id:
        return

    race_result_ids = []
    if stat_list_id is None:
        race_result_ids = _firstcycling_race_result_map_ids(
            first_cycling_race_id=first_cycling_race_id,
            first_cycling_stage_id=first_cycling_stage_id,
        )
        if not race_result_ids:
            return

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            columns = {
                column_name
                for column_name in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'stat_change_snapshot'
                        """
                    )
                ).scalars()
            }
            if 'race_result_id' not in columns or 'stat_list_id' not in columns:
                return

            update_fields = []
            where_clause, params = _discord_snapshot_scope_where_clause(
                race_result_ids=race_result_ids,
                stat_list_id=stat_list_id,
            )
            if not where_clause:
                return
            if 'discord_thread_id' in columns:
                update_fields.append('discord_thread_id = :thread_id')
                params['thread_id'] = str(thread_id)
            if 'discord_thread_url' in columns:
                update_fields.append('discord_thread_url = :thread_url')
                params['thread_url'] = thread_url
            if 'discord_notified_at' in columns:
                update_fields.append('discord_notified_at = CURRENT_TIMESTAMP')

            if not update_fields:
                return

            conn.execute(
                text(
                    f"""
                    UPDATE stat_change_snapshot
                    SET {', '.join(update_fields)}
                    WHERE {where_clause}
                    """
                ),
                params,
            )
    finally:
        engine.dispose()


def _discord_thread_url(thread_payload):
    guild_id = thread_payload.get('guild_id') if isinstance(thread_payload, dict) else None
    thread_id = thread_payload.get('id') if isinstance(thread_payload, dict) else None
    if not guild_id or not thread_id:
        return None
    return f'https://discord.com/channels/{guild_id}/{thread_id}'


def _format_discord_date(value):
    if value is None:
        return '-'
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _format_discord_username(username):
    cleaned = (username or '').strip()
    return cleaned or 'Unknown user'


def _discord_markdown_link(label, target_url):
    safe_label = str(label or '').replace('[', '\\[').replace(']', '\\]')
    safe_url = str(target_url or '').replace(')', '%29')
    return f'[{safe_label}]({safe_url})' if safe_url else safe_label


def _namespace_dashboard_url(namespace):
    if not has_request_context() or namespace is None:
        return None
    namespace_key = namespace.slug or namespace.id
    return url_for('web.dashboard', namespace=namespace_key, _external=True)


def _discord_thread_scope_filter(query, first_cycling_race_id=None, first_cycling_stage_id=None, stat_list_id=None):
    if first_cycling_race_id is None:
        query = query.filter(DiscordWorkflowThread.first_cycling_race_id.is_(None))
    else:
        query = query.filter(DiscordWorkflowThread.first_cycling_race_id == first_cycling_race_id)

    if first_cycling_stage_id is None:
        query = query.filter(DiscordWorkflowThread.first_cycling_stage_id.is_(None))
    else:
        query = query.filter(DiscordWorkflowThread.first_cycling_stage_id == first_cycling_stage_id)

    if stat_list_id is None:
        query = query.filter(DiscordWorkflowThread.stat_list_id.is_(None))
    else:
        query = query.filter(DiscordWorkflowThread.stat_list_id == stat_list_id)

    return query


def _load_discord_workflow_thread(
    namespace,
    settings,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    query = DiscordWorkflowThread.query.filter_by(
        namespace_id=namespace.id,
        discord_settings_id=settings.id,
        workflow=settings.workflow,
    )
    return _discord_thread_scope_filter(
        query,
        first_cycling_race_id=first_cycling_race_id,
        first_cycling_stage_id=first_cycling_stage_id,
        stat_list_id=stat_list_id,
    ).first()


def _save_discord_workflow_thread(
    namespace,
    settings,
    thread_id,
    thread_url=None,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    thread = _load_discord_workflow_thread(
        namespace,
        settings,
        first_cycling_race_id=first_cycling_race_id,
        first_cycling_stage_id=first_cycling_stage_id,
        stat_list_id=stat_list_id,
    )
    if thread is None:
        thread = DiscordWorkflowThread(
            namespace_id=namespace.id,
            discord_settings_id=settings.id,
            workflow=settings.workflow,
            first_cycling_race_id=first_cycling_race_id,
            first_cycling_stage_id=first_cycling_stage_id,
            stat_list_id=stat_list_id,
        )
        db.session.add(thread)

    thread.thread_id = str(thread_id)
    thread.thread_url = thread_url
    db.session.commit()
    return thread


def notify_discord_stat_scope_saved(
    namespace,
    scope_title,
    review_url,
    saved_count,
    saved_by=None,
    scope_label='Stat edits',
    race_date=None,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    workflow = DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS
    settings_rows = get_discord_settings_for_namespace_workflow(namespace, workflow)
    if not settings_rows:
        return False

    client = DiscordClient()
    if not client.bot_token:
        return False

    scope_state = _load_namespace_discord_scope_state(
        namespace,
        first_cycling_race_id=first_cycling_race_id,
        first_cycling_stage_id=first_cycling_stage_id,
        stat_list_id=stat_list_id,
    )
    if not scope_state:
        return False

    sent_any = False
    for settings in settings_rows:
        if not settings.channel_id:
            continue

        try:
            mention = f'<@&{settings.approver_role_id}>' if settings.approver_role_id else None
            approval_line = f'Awaiting approval by {mention}' if mention else 'Awaiting approval'
            save_message = (
                f'💾 by {_format_discord_username(saved_by)}.\n'
                f'{approval_line}'
            )
            workflow_thread = _load_discord_workflow_thread(
                namespace,
                settings,
                first_cycling_race_id=first_cycling_race_id,
                first_cycling_stage_id=first_cycling_stage_id,
                stat_list_id=stat_list_id,
            )

            if workflow_thread is not None:
                client.send_message(workflow_thread.thread_id, save_message)
                sent_any = True
                continue

            thread_payload = client.create_thread(settings.channel_id, f'Stat Edits: {scope_title}')
            thread_id = thread_payload.get('id')
            if not thread_id:
                raise DiscordIntegrationError('Discord did not return a thread ID.')

            thread_url = _discord_thread_url(thread_payload)
            namespace_label = f'Namespace: {namespace.slug or namespace.name or namespace.id}'
            namespace_link = _discord_markdown_link(namespace_label, _namespace_dashboard_url(namespace))
            scope_prefix = 'List' if stat_list_id is not None else 'Race'
            scope_link = _discord_markdown_link(f'{scope_prefix}: {scope_title}', review_url)
            initial_message = (
                f'{namespace_link}\n'
                f'Race Date: {_format_discord_date(race_date)}\n'
                f'{scope_link}'
            )
            client.send_message(thread_id, initial_message)
            client.send_message(thread_id, save_message)
            _save_discord_workflow_thread(
                namespace,
                settings,
                thread_id,
                thread_url,
                first_cycling_race_id=first_cycling_race_id,
                first_cycling_stage_id=first_cycling_stage_id,
                stat_list_id=stat_list_id,
            )
            sent_any = True
        except Exception:
            logger.exception('Failed to send Discord saved notification for settings row %s', settings.id)

    return sent_any


def notify_discord_stat_list_saved(namespace, list_id, list_name, review_url, saved_count, saved_by=None):
    return notify_discord_stat_scope_saved(
        namespace,
        list_name or f'Stat List #{list_id}',
        review_url,
        saved_count,
        saved_by=saved_by,
        scope_label='Stat List',
        stat_list_id=list_id,
    )


def load_namespace_stat_list_name(namespace, list_id):
    if namespace is None or list_id is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT list_name FROM stat_lists WHERE id = :list_id"),
                {'list_id': list_id},
            ).scalar()
    finally:
        engine.dispose()


def notify_discord_race_stat_saved(
    namespace,
    race_id,
    race_name,
    race_date,
    stage_id,
    review_url,
    saved_count,
    saved_by=None,
):
    if stage_id is not None:
        return False

    race_title = race_name or f'Race #{race_id}'
    return notify_discord_stat_scope_saved(
        namespace,
        race_title,
        review_url,
        saved_count,
        saved_by=saved_by,
        scope_label='Race Result',
        race_date=race_date,
        first_cycling_race_id=race_id,
        first_cycling_stage_id=None,
    )


def _post_discord_scope_message(
    namespace,
    content,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
    archive_thread=False,
):
    workflow = DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS
    settings_rows = get_discord_settings_for_namespace_workflow(namespace, workflow)
    if not settings_rows:
        return False
    client = DiscordClient()
    if not client.bot_token:
        return False

    sent_any = False
    for settings in settings_rows:
        try:
            workflow_thread = _load_discord_workflow_thread(
                namespace,
                settings,
                first_cycling_race_id=first_cycling_race_id,
                first_cycling_stage_id=first_cycling_stage_id,
                stat_list_id=stat_list_id,
            )
            if workflow_thread is None:
                continue

            client.send_message(workflow_thread.thread_id, content)
            if archive_thread:
                client.archive_thread(workflow_thread.thread_id)
            sent_any = True
        except Exception:
            logger.exception('Failed to send Discord workflow message for settings row %s', settings.id)
    return sent_any


def notify_discord_stat_list_approved(namespace, list_id, approved_by=None):
    return _post_discord_scope_message(
        namespace,
        f'✅ Approved by {_format_discord_username(approved_by)}',
        stat_list_id=list_id,
        archive_thread=True,
    )


def notify_discord_race_stat_approved(namespace, race_id, stage_id, approved_by=None):
    if stage_id is not None:
        return False

    return _post_discord_scope_message(
        namespace,
        f'✅ Approved by {_format_discord_username(approved_by)}',
        first_cycling_race_id=race_id,
        first_cycling_stage_id=None,
        archive_thread=True,
    )


def notify_discord_release_review_requested(namespace, release, release_url, overview_file_path):
    workflow = DiscordSettings.WORKFLOW_RELEASE_APPROVALS
    settings_rows = get_discord_settings_for_namespace_workflow(namespace, workflow)
    if not settings_rows:
        return False

    client = DiscordClient()
    if not client.bot_token:
        return False

    release_label = f'{release.release_name or "Release"} - {release.version or ""}'.strip()
    release_link = _discord_markdown_link(release_label, release_url)
    sent_any = False
    for settings in settings_rows:
        if not settings.channel_id:
            continue
        try:
            mention = f'<@&{settings.approver_role_id}> ' if settings.approver_role_id else ''
            content = f'Release Review Requested {mention}{release_link}'
            client.send_message_with_file(
                settings.channel_id,
                content,
                overview_file_path,
                file_name=os.path.basename(overview_file_path),
            )
            sent_any = True
        except Exception:
            logger.exception('Failed to send Discord release review notification for settings row %s', settings.id)
    return sent_any


def test_discord_channel_permissions(channel_id):
    client = DiscordClient()
    if not client.bot_token:
        raise DiscordIntegrationError('Discord bot token is required for the test.')
    if not channel_id:
        raise DiscordIntegrationError('Discord channel ID is required for the test.')

    thread_payload = client.create_thread(channel_id, 'WorldDB Discord Test')
    thread_id = thread_payload.get('id')
    if not thread_id:
        raise DiscordIntegrationError('Discord did not return a test thread ID.')

    client.send_message(
        thread_id,
        'WorldDB Discord integration test succeeded. The bot can create threads and post messages in this channel.',
    )
    return thread_id
