FROM docker.io/asksven/python-dev:v3.14.8

ARG UV_VERSION=0.8.4

# The Chainguard -dev image defaults to a non-root UID (65532); steward needs
# root at build (apk add) and at runtime (entrypoint writes /etc/passwd, installs
# crontab to /etc/cron, chmods, and drops to root by default with STEWARD_UID=0).
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