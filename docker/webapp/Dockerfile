FROM node:22-bookworm-slim AS cdb-converter

WORKDIR /opt/cdb-converter
RUN npm install --no-save --no-audit --no-fund cdb-converter@0.4.1

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    gcc \
    python3-dev \
    libpq-dev \
    curl \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Share the same PCM CDB conversion library used by the desktop Activity Monitor.
COPY --from=cdb-converter /usr/local/bin/node /usr/local/bin/node
COPY --from=cdb-converter /opt/cdb-converter/node_modules /opt/cdb-converter/node_modules

# Copy application code
COPY webapp/ /app/webapp/
COPY wsgi.py /app/wsgi.py

# Create data directories
RUN mkdir -p /app/filestore

# Expose port
EXPOSE 6000

# Default command
CMD ["gunicorn", "--bind", "0.0.0.0:6000", "--workers", "2", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
