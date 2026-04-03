#!/bin/bash
set -e

echo "Waiting for PostgreSQL server to be ready..."
echo "Connecting to: database:5432 as user ${POSTGRES_USER}"

# Wait with timeout
RETRIES=30
COUNT=0
until pg_isready -h database -U ${POSTGRES_USER} > /dev/null 2>&1; do
    COUNT=$((COUNT + 1))
    if [ $COUNT -ge $RETRIES ]; then
        echo "ERROR: PostgreSQL did not become ready in time"
        echo "Debug info:"
        echo "POSTGRES_USER=${POSTGRES_USER}"
        echo "POSTGRES_DB=${POSTGRES_DB}"
        echo "Attempting direct connection test..."
        pg_isready -h database -U ${POSTGRES_USER}
        exit 1
    fi
    echo "Waiting for postgres... (attempt $COUNT/$RETRIES)"
    sleep 2
done
echo "PostgreSQL server is ready!"

# Always run init-db at startup. The command is designed to be idempotent.
echo "Running idempotent database initialization..."
flask init-db
echo "Database initialization step complete!"

# Execute the main command (flask run, celery worker, etc.)
exec "$@"
