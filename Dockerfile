FROM docker.io/asksven/python-dev:v3.14.8

ARG UV_VERSION=0.8.4

# steward must run as root: its entrypoint writes /etc/passwd for arbitrary host
# UIDs, installs crontab to /etc/cron, chmods, and drops to root by default
# (STEWARD_UID=0). The Chainguard -dev image defaults to non-root UID 65532.
# hadolint ignore=DL3002
USER root

# Install system dependencies (wolfi/apk). Note: docker-compose is the package
# name in the wolfi base - the upstream `docker-cli-compose` does not exist there.
RUN apk add --no-cache \
    git=~2 \
    docker-cli=~29 \
    docker-compose=~5 \
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