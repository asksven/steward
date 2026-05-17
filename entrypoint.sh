#!/bin/sh
set -e

STEWARD_UID=${STEWARD_UID:-0}
STEWARD_GID=${STEWARD_GID:-$STEWARD_UID}

echo "steward starting"
echo "  Node:         ${GITOPS_NODE_NAME:-$(hostname)}"
echo "  Control repo: ${CONTROL_REPO_URL}"
echo "  Gitops root:  ${GITOPS_ROOT:-/git}"
echo "  Running as:   ${STEWARD_UID}:${STEWARD_GID}"

# Copy SSH credentials from staging mount to writable dir with correct root ownership
# (bind-mounted files retain host UID; SSH rejects keys/config not owned by the running user)
if [ -d /root/.ssh-host ] && ls /root/.ssh-host/* >/dev/null 2>&1; then
  rm -rf /root/.ssh
  mkdir -p /root/.ssh
  cp /root/.ssh-host/* /root/.ssh/
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/*
fi

# Ensure git root directories exist and are owned by the target user
mkdir -p "${GITOPS_ROOT:-/git}/stacks"
chown -R "${STEWARD_UID}:${STEWARD_GID}" "${GITOPS_ROOT:-/git}"

if [ "${STEWARD_UID}" != "0" ]; then
  # Allow the non-root process to reach the Docker socket
  chmod o+rw /var/run/docker.sock

  # Generate a runtime crontab that runs jobs as the target user
  cat > /tmp/crontab-runtime <<EOF
* * * * * su-exec ${STEWARD_UID}:${STEWARD_GID} python3 /app/steward.py reconcile >> /proc/1/fd/1 2>&1
0 * * * * su-exec ${STEWARD_UID}:${STEWARD_GID} python3 /app/steward.py self-update >> /proc/1/fd/1 2>&1
EOF
  crontab /tmp/crontab-runtime
  echo "Crontab installed"

  echo "Running initial reconciliation..."
  su-exec "${STEWARD_UID}:${STEWARD_GID}" python3 /app/steward.py reconcile
else
  crontab /etc/cron/crontab
  echo "Crontab installed"

  echo "Running initial reconciliation..."
  python3 /app/steward.py reconcile
fi

# Start cron in foreground, log to stdout
echo "Starting crond..."
exec crond -f -l 6
