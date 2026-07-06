"""Discord integration scaffold."""

import json
import logging
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request
import uuid

from flask import current_app, has_app_context, has_request_context, url_for
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import DiscordSettings, DiscordWorkflowThread, RaceResultMap
from webapp.models.main.firstcycling import FirstCyclingRace, FirstCyclingRaceResult, FirstCyclingRaceStage

logger = logging.getLogger('discord')

STAT_APPROVAL_IMAGE_FIELDS = (
    'mo',
    'mm',
    'dh',
    'hil',
    'fla',
    'cob',
    'tt',
    'prl',
    'spr',
    'acc',
    'end',
    'res',
    'rec',
    'att',
)

STAT_APPROVAL_IMAGE_DISPLAY_FIELDS = (
    'fla',
    'mo',
    'mm',
    'hil',
    'dh',
    'cob',
    'tt',
    'prl',
    'spr',
    'acc',
    'end',
    'res',
    'rec',
    'att',
)

STAT_APPROVAL_IMAGE_COLUMNS = {stat_name: f'stat_{stat_name}' for stat_name in STAT_APPROVAL_IMAGE_FIELDS}


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


def _stat_image_value_class(value):
    if value is None:
        return 'empty'
    if value <= 65:
        return 'low'
    if 66 <= value <= 69:
        return 'decent'
    if 70 <= value <= 73:
        return 'good'
    if 74 <= value <= 77:
        return 'very_good'
    if 78 <= value <= 81:
        return 'excellent'
    return 'elite'


def _coerce_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finish_position_label(position):
    position = _coerce_int(position)
    if position is None:
        return None
    labels = {
        999: 'DNF',
        998: 'DNS',
        997: 'OTL',
        996: 'DSQ',
    }
    return labels.get(position, str(position))


def _pick_race_result_rows(races):
    result_rows = []
    for race in races:
        race_rows = FirstCyclingRaceResult.query.filter_by(
            race_id=race.id,
            result_scope='race',
            stage_id=None,
        ).all()
        if race_rows:
            result_rows.extend(race_rows)
            continue

        final_stage = FirstCyclingRaceStage.query.filter_by(race_id=race.id).order_by(
            FirstCyclingRaceStage.stage_number.desc().nullslast(),
            FirstCyclingRaceStage.id.desc(),
        ).first()
        if final_stage is None:
            continue

        result_rows.extend(
            FirstCyclingRaceResult.query.filter_by(
                race_id=race.id,
                stage_id=final_stage.id,
            ).all()
        )

    return result_rows


def _load_stat_approval_image_rows(
    namespace,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    if namespace is None:
        return []

    race_result_ids = []
    if stat_list_id is None:
        race_result_ids = _firstcycling_race_result_map_ids(
            first_cycling_race_id=first_cycling_race_id,
            first_cycling_stage_id=first_cycling_stage_id,
        )
        if not race_result_ids:
            return []

    where_clause, params = _discord_snapshot_scope_where_clause(
        race_result_ids=race_result_ids,
        stat_list_id=stat_list_id,
    )
    if not where_clause:
        return []

    snapshot_select = ', '.join(
        f's.{column_name} AS {column_name}'
        for column_name in STAT_APPROVAL_IMAGE_COLUMNS.values()
    )
    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            changed_rows = conn.execute(
                text(
                    f"""
                    SELECT
                        s.cdb_cyclist_id,
                        COALESCE(
                            NULLIF(c.first_last_name, ''),
                            NULLIF(CONCAT_WS(' ', c.first_name, c.last_name), ''),
                            'Rider #' || c.id::text
                        ) AS rider_name,
                        COALESCE(t.team_name, t.short_name, '-') AS team_name,
                        {snapshot_select}
                    FROM stat_change_snapshot s
                    JOIN (
                        SELECT DISTINCT cdb_cyclist_id
                        FROM stat_changes
                        WHERE {where_clause}
                    ) changed ON changed.cdb_cyclist_id = s.cdb_cyclist_id
                    LEFT JOIN cdb_cyclist c ON c.id = s.cdb_cyclist_id
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    WHERE {where_clause}
                    ORDER BY rider_name ASC, s.cdb_cyclist_id ASC
                    """
                ),
                params,
            ).mappings().all()

            stat_change_rows = conn.execute(
                text(
                    f"""
                    SELECT cdb_cyclist_id, stat_name, final_value, delta_value
                    FROM stat_changes
                    WHERE {where_clause}
                    ORDER BY id ASC
                    """
                ),
                params,
            ).mappings().all()
    finally:
        engine.dispose()

    changes_by_rider = {}
    for change in stat_change_rows:
        rider_id = change.get('cdb_cyclist_id')
        stat_name = (change.get('stat_name') or '').strip().lower()
        if rider_id is None or stat_name not in STAT_APPROVAL_IMAGE_FIELDS:
            continue
        changes_by_rider.setdefault(rider_id, {})[stat_name] = {
            'final_value': change.get('final_value'),
            'delta_value': change.get('delta_value'),
        }

    rows = []
    for row in changed_rows:
        rider_id = row.get('cdb_cyclist_id')
        changes = changes_by_rider.get(rider_id) or {}
        stats = {}
        for stat_name, column_name in STAT_APPROVAL_IMAGE_COLUMNS.items():
            stats[stat_name] = row.get(column_name)
        for stat_name, change in changes.items():
            if change.get('final_value') is not None:
                stats[stat_name] = change.get('final_value')
        rows.append({
            'cdb_cyclist_id': rider_id,
            'rider_name': row.get('rider_name') or f'Rider #{rider_id}',
            'team_name': row.get('team_name') or '-',
            'stats': stats,
            'changes': changes,
        })

    if rows and stat_list_id is None and first_cycling_race_id is not None:
        cdb_to_fc = {}
        connection_string = namespace.build_connection_string()
        engine = create_engine(connection_string)
        try:
            with engine.connect() as conn:
                link_rows = conn.execute(
                    text(
                        """
                        SELECT id, first_cycling_id
                        FROM cdb_cyclist
                        WHERE id = ANY(:cdb_cyclist_ids)
                        """
                    ),
                    {'cdb_cyclist_ids': [row['cdb_cyclist_id'] for row in rows]},
                ).mappings().all()
            cdb_to_fc = {
                link_row.get('id'): _coerce_int(link_row.get('first_cycling_id'))
                for link_row in link_rows
            }
        finally:
            engine.dispose()

        changed_fc_ids = {
            fc_id
            for fc_id in cdb_to_fc.values()
            if fc_id is not None
        }
        races = FirstCyclingRace.query.filter_by(race_id=first_cycling_race_id).order_by(
            FirstCyclingRace.season_year.desc(),
            FirstCyclingRace.id.desc(),
        ).all()
        result_rows = _pick_race_result_rows(races)
        result_by_rider = {}
        for result in result_rows:
            rider_id = _coerce_int(result.rider_id)
            if rider_id in changed_fc_ids and rider_id not in result_by_rider:
                result_by_rider[rider_id] = result

        for row in rows:
            result = result_by_rider.get(cdb_to_fc.get(row['cdb_cyclist_id']))
            if result is None:
                row['position'] = None
                row['position_label'] = None
                row['time'] = None
                continue
            position = result.overall_position if result.overall_position is not None else result.stage_position
            time_value = result.overall_time if result.overall_time else result.stage_time
            row['position'] = position
            row['position_label'] = _finish_position_label(position)
            row['time'] = time_value
        rows.sort(key=lambda row: (
            row.get('position') is None,
            row.get('position') if row.get('position') is not None else 999999,
            row['rider_name'],
        ))

    return rows


def _draw_fit_text(draw, box, text_value, font, fill, min_size=9, anchor='mm'):
    text_value = str(text_value or '')
    x1, y1, x2, y2 = box
    max_width = max(1, x2 - x1 - 8)
    max_height = max(1, y2 - y1 - 4)
    current_font = font
    try:
        from PIL import ImageFont
        font_path = getattr(font, 'path', None)
        size = getattr(font, 'size', 12)
        while size > min_size:
            bbox = draw.textbbox((0, 0), text_value, font=current_font)
            if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
                break
            size -= 1
            current_font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    except Exception:
        pass

    if anchor == 'lm':
        xy = (x1 + 6, (y1 + y2) / 2)
    else:
        xy = ((x1 + x2) / 2, (y1 + y2) / 2)
    draw.text(xy, text_value, font=current_font, fill=fill, anchor=anchor)


def _load_stat_image_font(font_name, size):
    try:
        from PIL import ImageFont
    except ImportError:
        return None

    candidates = [
        font_name,
        os.path.join('/usr/share/fonts/truetype/dejavu', font_name),
        os.path.join('/usr/local/share/fonts', font_name),
        'arialbd.ttf' if 'Bold' in font_name else 'arial.ttf',
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render_stat_approval_image(rows, scope_title, include_race_columns=False):
    if not rows:
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning('Pillow is not installed; Discord stat approval image skipped.')
        return None

    render_scale = 2
    font_regular = _load_stat_image_font('DejaVuSans.ttf', 13 * render_scale)
    font_bold = _load_stat_image_font('DejaVuSans-Bold.ttf', 13 * render_scale)
    font_small = _load_stat_image_font('DejaVuSans.ttf', 11 * render_scale)
    font_small_bold = _load_stat_image_font('DejaVuSans-Bold.ttf', 11 * render_scale)

    stat_width = 72 * render_scale
    columns = []
    if include_race_columns:
        columns.extend([
            ('Pos.', 60 * render_scale),
            ('Rider', 190 * render_scale),
            ('Team', 180 * render_scale),
            ('Time', 82 * render_scale),
        ])
    else:
        columns.extend([
            ('Rider', 220 * render_scale),
            ('Team', 190 * render_scale),
        ])
    columns.extend([(stat_name.upper(), stat_width) for stat_name in STAT_APPROVAL_IMAGE_DISPLAY_FIELDS])

    header_height = 36 * render_scale
    row_height = 36 * render_scale
    margin = 16 * render_scale
    title_height = 34 * render_scale
    width = sum(width for _, width in columns) + margin * 2
    height = title_height + header_height + row_height * len(rows) + margin * 2

    palette = {
        'background': '#ffffff',
        'border': '#dee2e6',
        'stripe': '#f8f9fa',
        'header': '#f1f3f5',
        'text': '#212529',
        'muted': '#6c757d',
        'low': '#6c757d',
        'decent': '#2563eb',
        'good': '#90ee90',
        'very_good': '#ffd700',
        'excellent': '#ff8c00',
        'elite': '#dc3545',
    }
    text_for_class = {
        'low': '#ffffff',
        'decent': '#ffffff',
        'good': '#000000',
        'very_good': '#000000',
        'excellent': '#000000',
        'elite': '#ffffff',
        'empty': palette['text'],
    }

    image = Image.new('RGB', (width, height), palette['background'])
    draw = ImageDraw.Draw(image)

    x = margin
    y = margin
    draw.text((x, y), scope_title or 'Stat Edits', font=font_bold, fill=palette['text'])
    y += title_height

    column_edges = []
    current_x = margin
    for label, column_width in columns:
        column_edges.append((label, current_x, current_x + column_width))
        draw.rectangle((current_x, y, current_x + column_width, y + header_height), fill=palette['header'], outline=palette['border'])
        _draw_fit_text(draw, (current_x, y, current_x + column_width, y + header_height), label, font_bold, palette['text'])
        current_x += column_width

    y += header_height
    for row_index, row in enumerate(rows):
        row_fill = palette['stripe'] if row_index % 2 else palette['background']
        draw.rectangle((margin, y, width - margin, y + row_height), fill=row_fill)
        cell_index = 0

        if include_race_columns:
            position_text = row.get('position_label') or '-'
            for text_value in (position_text, row.get('rider_name'), row.get('team_name'), row.get('time') or '-'):
                _, x1, x2 = column_edges[cell_index]
                draw.rectangle((x1, y, x2, y + row_height), outline=palette['border'])
                anchor = 'lm' if cell_index in (1, 2) else 'mm'
                _draw_fit_text(draw, (x1, y, x2, y + row_height), text_value, font_regular, palette['text'], anchor=anchor)
                cell_index += 1
        else:
            for text_value in (row.get('rider_name'), row.get('team_name')):
                _, x1, x2 = column_edges[cell_index]
                draw.rectangle((x1, y, x2, y + row_height), outline=palette['border'])
                _draw_fit_text(draw, (x1, y, x2, y + row_height), text_value, font_regular, palette['text'], anchor='lm')
                cell_index += 1

        for stat_name in STAT_APPROVAL_IMAGE_DISPLAY_FIELDS:
            _, x1, x2 = column_edges[cell_index]
            value = row.get('stats', {}).get(stat_name)
            change = row.get('changes', {}).get(stat_name)
            value_class = _stat_image_value_class(value)
            fill = palette.get(value_class, row_fill)
            text_fill = text_for_class.get(value_class, palette['text'])
            if value is None:
                draw.rectangle((x1, y, x2, y + row_height), fill=fill, outline=palette['border'])
                draw.text(((x1 + x2) / 2, y + row_height / 2), '-', font=font_regular, fill=text_fill, anchor='mm')
            else:
                split_x = x1 + (30 * render_scale)
                draw.rectangle((x1, y, split_x, y + row_height), fill=palette['background'], outline=palette['border'])
                draw.rectangle((split_x, y, x2, y + row_height), fill=fill, outline=palette['border'])
                draw.line((split_x, y, split_x, y + row_height), fill=palette['border'])
                if change is not None and change.get('delta_value'):
                    delta = change.get('delta_value')
                    delta_text = f'+{delta}' if delta > 0 else str(delta)
                    delta_fill = '#198754' if delta > 0 else '#dc3545'
                    draw.text(((x1 + split_x) / 2, y + row_height / 2), delta_text, font=font_bold, fill=delta_fill, anchor='mm')
                draw.text(((split_x + x2) / 2, y + row_height / 2), str(value), font=font_regular, fill=text_fill, anchor='mm')
            cell_index += 1

        y += row_height

    output = tempfile.NamedTemporaryFile(prefix='worlddb-stat-approval-', suffix='.png', delete=False)
    output.close()
    image.save(output.name, 'PNG')
    return output.name


def _build_stat_approval_image(
    namespace,
    scope_title,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
):
    rows = _load_stat_approval_image_rows(
        namespace,
        first_cycling_race_id=first_cycling_race_id,
        first_cycling_stage_id=first_cycling_stage_id,
        stat_list_id=stat_list_id,
    )
    return _render_stat_approval_image(
        rows,
        scope_title,
        include_race_columns=stat_list_id is None,
    )


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
    approval_image_path = None
    try:
        approval_image_path = _build_stat_approval_image(
            namespace,
            scope_title,
            first_cycling_race_id=first_cycling_race_id,
            first_cycling_stage_id=first_cycling_stage_id,
            stat_list_id=stat_list_id,
        )
    except Exception:
        logger.exception('Failed to generate Discord stat approval image for %s', scope_title)
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
                if approval_image_path:
                    client.send_message_with_file(
                        workflow_thread.thread_id,
                        save_message,
                        approval_image_path,
                        file_name='stat-approval.png',
                    )
                else:
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
            scope_label = f'{scope_prefix}: {scope_title}'
            if stat_list_id is None and race_date is not None:
                scope_label = f'{_format_discord_date(race_date)} - {scope_title}'
            scope_link = _discord_markdown_link(scope_label, review_url)
            initial_message = (
                f'{namespace_link}\n'
                f'{scope_link}'
            )
            client.send_message(thread_id, initial_message)
            if approval_image_path:
                client.send_message_with_file(
                    thread_id,
                    save_message,
                    approval_image_path,
                    file_name='stat-approval.png',
                )
            else:
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

    if approval_image_path:
        try:
            os.unlink(approval_image_path)
        except OSError:
            logger.warning('Could not delete temporary Discord stat approval image %s', approval_image_path)

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
