FROM python:3.12-alpine

# Install system dependencies
RUN apk add --no-cache \
    git \
    docker-cli \
    openssh-client \
    su-exec \
    && pip install --no-cache-dir \
    gitpython \
    pyyaml

# Install app
WORKDIR /app
COPY steward.py .

# Install crontab
COPY crontab /etc/cron/crontab

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
