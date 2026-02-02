# PCM Manager

A Flask-based web application for managing Pro Cycling Manager (PCM) mod databases with multi-namespace architecture.

## Architecture Overview

### Multi-Database Structure
- **Main Database**: Stores users, roles, permissions, and namespace registry
- **Namespace Databases**: One PostgreSQL database per CDB instance, containing riders, teams, races, palmares, photos, and stats
- **SQLite CDB Files**: PCM-compatible game database files (input/output)

### Five Application panels
1. **Stat Manager**: Edit rider/team/race statistics
2. **Photo Manager**: Upload and manage photos for entities
3. **Palmares Manager**: Manage career achievements with API integration
4. **Release Manager**: Create namespaces and export CDB releases
5. **Admin**: User and role management

### Four User Roles
- **Admin**: Full system access, user management
- **Release Manager**: Create namespaces, publish releases
- **Stat Editor**: Edit statistics, import palmares data
- **Viewer**: Read-only access

## Setup Instructions

### Quick Start

1. Clone and configure:
   ```bash
   git clone <repository-url>
   cd pcm-manager
   cp .env.example .env
   # Edit .env to set ADMIN_USERNAME and ADMIN_PASSWORD (optional)
   ```

2. Start services:
   ```bash
   docker compose up -d --build
   ```

3. Access at http://localhost

### Docker Commands

```bash
# View logs
docker compose logs -f webapp

# Stop services
docker compose down

# Fresh start (removes data)
docker compose down -v

# Run Flask commands
docker compose exec webapp webapp create-admin
```

## Development Status

- (DONE) Design and build webapp architecutre and tech stack
- (DONE) Build basic user management config (create user, change user password, add/remove user roles)
- (DONE) Build namespace management config (create namespace, import cdb into namespace, interal cdb model and table mappings)
- (NOT STARTED) Create Stat Manager Panel (generate race result from FC, view cyclist stats from race result, modify stats)
- (NOT STARTED) Create Palmares Manager Panel


## Configuration

See `.env.example` for all environment variables.

## License

MIT License
