"""Discord integration scaffold."""

import logging
import os

from flask import current_app, has_app_context

logger = logging.getLogger('discord')


class DiscordIntegrationError(Exception):
    """Raised when Discord integration configuration or execution fails."""


class DiscordClient:
    """Minimal scaffold for future Discord integration work."""

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

    def send_message(self, channel_reference, content):
        """Placeholder method for a future Discord message send flow."""
        raise DiscordIntegrationError(
            'Discord integration is scaffolded but send_message is not implemented yet.'
        )
