# WorldDB Online

WorldDB is a web application for managing Pro Cycling Manager (PCM) mod databases. It supports editing game data, reviewing rider stat changes, and preparing CDB releases across maintained namespaces.

## Quick Start

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

See the [webapp README](webapp/README.md) for architecture, roles, Docker commands, and Discord setup, and [Contributing](contribution.md) for the development workflow.

## License

MIT License
