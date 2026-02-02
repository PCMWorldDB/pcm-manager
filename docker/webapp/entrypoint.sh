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

# Check if database tables are initialized
echo "Checking if database is initialized..."
TABLE_EXISTS=$(PGPASSWORD=${POSTGRES_PASSWORD} psql -h database -U ${POSTGRES_USER} -d ${POSTGRES_DB} -tAc "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'users');")

if [ "$TABLE_EXISTS" = "f" ]; then
    echo "Database not initialized. Running init-db..."
    flask init-db
    echo "Database initialization complete!"
else
    echo "Database already initialized."
fi

# Execute the main command (flask run, celery worker, etc.)
exec "$@"
