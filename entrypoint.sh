#!/bin/sh
set -e

echo "steward starting"
echo "  Node:         ${GITOPS_NODE_NAME:-$(hostname)}"
echo "  Control repo: ${CONTROL_REPO_URL}"
echo "  Gitops root:  ${GITOPS_ROOT:-~/git}"

# Copy SSH credentials from staging mount to writable dir with correct root ownership
# (bind-mounted files retain host UID; SSH rejects keys/config not owned by the running user)
if [ -d /root/.ssh-host ] && ls /root/.ssh-host/* >/dev/null 2>&1; then
  rm -rf /root/.ssh
  mkdir -p /root/.ssh
  cp /root/.ssh-host/* /root/.ssh/
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/*
fi

# Ensure git root directories exist
mkdir -p "${GITOPS_ROOT:-$HOME/git}/stacks"

# Install crontab
crontab /etc/cron/crontab
echo "Crontab installed"

# Run an immediate reconciliation on startup so we don't wait for the first cron tick
echo "Running initial reconciliation..."
python3 /app/steward.py reconcile

# Start cron in foreground, log to stdout
echo "Starting crond..."
exec crond -f -l 6
