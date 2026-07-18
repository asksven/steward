FROM python:3.14-alpine

ARG UV_VERSION=0.8.4

# Install system dependencies
RUN apk add --no-cache \
    git=~2 \
    docker-cli=~29 \
    docker-cli-compose=~5 \
    openssh-client=~10 \
    su-exec=~0.3 && \
    pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache-dir -r requirements.txt

# Install app
COPY steward.py .
COPY metrics_server.py .
COPY state_store.py .

# Install crontab
COPY crontab /etc/cron/crontab

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
