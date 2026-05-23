FROM python:3.14-alpine

# Install system dependencies
RUN apk add --no-cache \
    git \
    docker-cli \
    docker-cli-compose \
    openssh-client \
    su-exec

# Install Python package manager and pinned runtime dependencies
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache-dir -r requirements.txt

# Install app
COPY steward.py .
COPY metrics_server.py .

# Install crontab
COPY crontab /etc/cron/crontab

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
