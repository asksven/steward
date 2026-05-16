#!/bin/sh
set -e

echo "steward starting"
echo "  Node:         ${GITOPS_NODE_NAME:-$(hostname)}"
echo "  Control repo: ${CONTROL_REPO_URL}"
echo "  Gitops root:  ${GITOPS_ROOT:-~/git}"

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
