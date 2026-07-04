## Overview

The Discord integration sends workflow notifications to configured Discord channels for namespace-specific review and approval activity.

## Access

- Users need Discord integration update permission, or admin access, to configure Discord workflow targets.
- The application environment must include `DISCORD_BOT_TOKEN` before testing or sending Discord messages.

## Selecting a Namespace

Discord settings are namespace-specific. Select the namespace from the side panel before editing Discord workflow targets.

## Workflow Targets

Each workflow target defines where a specific type of notification should be sent.

- Name is the display name for the target.
- Workflow selects the notification workflow.
- Enabled controls whether WorldDB sends notifications for that target.
- Channel ID is the Discord channel that receives messages.
- Approver Role ID is optional and can be used for role mentions.

## Creating or Updating a Target

1. Open Settings, then select Integrations.
2. Select the Discord provider tab.
3. Confirm the selected namespace.
4. Enter a name, workflow, channel ID, and optional approver role ID.
5. Enable the target when it should receive notifications.
6. Save the workflow target.

## Testing a Target

Use the test action to confirm that the bot can access the configured Discord channel.

> A successful test can create a test thread. If the test fails, check the channel ID, bot permissions, and `DISCORD_BOT_TOKEN` environment value.
