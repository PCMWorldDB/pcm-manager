# Discord

`DiscordClient` wraps outbound Discord bot API calls. Workflow helpers send
notifications for saved and approved rider stats and release review requests,
including generated stat approval images and workflow threads.

## Configuration

- `DISCORD_BOT_TOKEN`: bot authentication for API requests.
- `DISCORD_WEBHOOK_URL`: optional client setting; bot API requests require the
  bot token.
- Namespace-specific channel and workflow settings are stored through
  `DiscordSettings`; thread records use `DiscordWorkflowThread`.

Keep tokens and webhook URLs out of committed resources and logs.

## Usage

```python
from webapp.integrations.discord import (
    DiscordClient,
    notify_discord_stat_list_saved,
    notify_discord_stat_list_approved,
)
```

The web routes call workflow helpers with the active Namespace and relevant
stat scope. These helpers use Flask context and database state, may update
workflow thread records, and send messages to Discord. Avoid calling them merely
to test imports. `test_discord_channel_permissions(channel_id)` checks configured
channel access using the bot.
