#!/bin/bash
set -e

# This script runs when PostgreSQL container is first initialized
# It only runs if /var/lib/postgresql/data is empty

echo "PostgreSQL initialization script starting..."

# Create the application database if it doesn't exist
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-EOSQL
    SELECT 'CREATE DATABASE ${POSTGRES_DB}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${POSTGRES_DB}')\gexec
EOSQL

echo "Database '${POSTGRES_DB}' ensured to exist."

# Enable required extensions if needed
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${POSTGRES_DB}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
EOSQL

echo "PostgreSQL initialization complete!"
