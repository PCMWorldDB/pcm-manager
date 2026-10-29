# WorldDB Webapp

## Architecture Overview

### Multi-Database Structure
- **Main Database**: Stores users, roles, permissions, and namespace registry
- **Namespace Databases**: One PostgreSQL database per CDB instance, containing riders, teams, races, palmares, photos, and stats
- **SQLite CDB Files**: PCM-compatible game database files (input/output)

### Application Workflows
- **Stats Editor**: Submit rider stat changes from race results or rider lists for validation
- **Releases**: Review changes and generate CDB release artifacts
- **Administration**: Manage users, roles, namespaces, and integrations

### User Roles

| Role | System name | Description |
| --- | --- | --- |
| Administrator | `admin` | Full system access |
| Integrations Manager | `integrations_manager` | Can view and run integrations and update Discord settings |
| Release Approver | `release_manager` | Can approve and publish releases |
| Release Editor | `release_editor` | Can create and edit releases |
| Stat Editor | `stat_editor` | Can edit rider statistics |
| Stat Editor Approver | `stat_editor_approver` | Can approve rider/team/race statistics edits |
| User Manager | `user_manager` | Can create users and update user account settings |
| Viewer | `viewer` | Read-only access to data |


```bash
docker compose down -v
```

## Discord Bot

Install the Discord bot to a guild with the required workflow notification permissions:

https://discord.com/oauth2/authorize?client_id=1505027515385647175&permissions=309237680192&integration_type=0&scope=bot

Set `DISCORD_BOT_TOKEN` in the application environment before testing or sending Discord notifications.

After the bot is installed, go to **Admin > Integrations > Discord** for the target namespace and configure:

- `Channel ID`: Discord channel where workflow threads/messages should be created
- `Approver Role ID`: Discord role to mention for approval notifications

Use the **Test** action in the Discord integration settings to confirm the bot can create a thread and post in the configured channel.
