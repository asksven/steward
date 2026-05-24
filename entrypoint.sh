#!/bin/sh
set -e

STEWARD_UID=${STEWARD_UID:-0}
STEWARD_GID=${STEWARD_GID:-$STEWARD_UID}

# Non-root users get their own home so SSH can find keys under a readable path.
# /root is drwx------ (700) — a process running as UID!=0 cannot traverse it.
if [ "${STEWARD_UID}" != "0" ]; then
  STEWARD_HOME=/home/steward
  # OpenSSH calls getpwuid() on startup and refuses to run if the UID has no
  # /etc/passwd entry. Add one dynamically so the container works with any host UID.
  if ! getent passwd "${STEWARD_UID}" >/dev/null 2>&1; then
    echo "steward:x:${STEWARD_UID}:${STEWARD_GID}::/home/steward:/bin/sh" >> /etc/passwd
  fi
  if ! getent group "${STEWARD_GID}" >/dev/null 2>&1; then
    echo "steward:x:${STEWARD_GID}:" >> /etc/group
  fi
else
  STEWARD_HOME=/root
fi

echo "steward starting"
echo "  Node:         ${GITOPS_NODE_NAME:-$(hostname)}"
echo "  Control repo: ${CONTROL_REPO_URL}"
echo "  Gitops root:  ${GITOPS_ROOT:-/git}"
echo "  Running as:   ${STEWARD_UID}:${STEWARD_GID}"
echo "  Home:         ${STEWARD_HOME}"
echo "  Metrics port: ${METRICS_PORT:-(disabled)}"

# Set up SSH from Docker secrets (/run/secrets/ssh_key, /run/secrets/ssh_known_hosts)
if [ -f /run/secrets/ssh_key ]; then
  mkdir -p "${STEWARD_HOME}/.ssh"
  cp /run/secrets/ssh_key "${STEWARD_HOME}/.ssh/id_ed25519"
  chmod 600 "${STEWARD_HOME}/.ssh/id_ed25519"

  if [ -f /run/secrets/ssh_known_hosts ]; then
    cp /run/secrets/ssh_known_hosts "${STEWARD_HOME}/.ssh/known_hosts"
    chmod 644 "${STEWARD_HOME}/.ssh/known_hosts"
    SSH_HOST_KEY_CHECK="yes"
  else
    echo "WARNING: No known_hosts secret provided; using accept-new for host key checking"
    SSH_HOST_KEY_CHECK="accept-new"
  fi

  # Write a minimal SSH config that uses the key for all hosts
  cat > "${STEWARD_HOME}/.ssh/config" <<SSHCONF
Host *
  IdentityFile ${STEWARD_HOME}/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking ${SSH_HOST_KEY_CHECK}
SSHCONF
  chmod 600 "${STEWARD_HOME}/.ssh/config"
  chmod 700 "${STEWARD_HOME}/.ssh"
  chown -R "${STEWARD_UID}:${STEWARD_GID}" "${STEWARD_HOME}/.ssh"
  echo "SSH key configured from Docker secret"
elif [ -d /root/.ssh-host ] && ls /root/.ssh-host/* >/dev/null 2>&1; then
  # Legacy fallback: copy from bind-mounted staging directory
  mkdir -p "${STEWARD_HOME}/.ssh"
  cp /root/.ssh-host/* "${STEWARD_HOME}/.ssh/"
  chmod 700 "${STEWARD_HOME}/.ssh"
  chmod 600 "${STEWARD_HOME}/.ssh/"*
  chown -R "${STEWARD_UID}:${STEWARD_GID}" "${STEWARD_HOME}"
  echo "SSH key configured from legacy .ssh-host mount (consider migrating to Docker secrets)"
else
  echo "WARNING: No SSH key found. Git operations on private repos will fail."
  echo "  Configure SSH_KEY_FILE in .env or provide /run/secrets/ssh_key"
fi

# Ensure git root directories exist and are owned by the target user
mkdir -p "${GITOPS_ROOT:-/git}/stacks"
chown -R "${STEWARD_UID}:${STEWARD_GID}" "${GITOPS_ROOT:-/git}"

# Start metrics server in background if METRICS_PORT is set
if [ -n "${METRICS_PORT:-}" ]; then
  python3 /app/metrics_server.py "${METRICS_PORT}" >> /proc/1/fd/1 2>&1 &
  echo "Metrics server started on port ${METRICS_PORT}"
fi

if [ "${STEWARD_UID}" != "0" ]; then
  # Allow the non-root process to reach the Docker socket
  chmod o+rw /var/run/docker.sock

  # Generate a runtime crontab that runs jobs as the target user with the correct HOME
  cat > /tmp/crontab-runtime <<EOF
* * * * * su-exec ${STEWARD_UID}:${STEWARD_GID} env HOME=${STEWARD_HOME} sh -lc 'cd /app && python3 /app/steward.py reconcile' >> /proc/1/fd/1 2>&1
EOF
  crontab /tmp/crontab-runtime
  echo "Crontab installed"

  echo "Running initial reconciliation..."
  su-exec "${STEWARD_UID}:${STEWARD_GID}" env HOME="${STEWARD_HOME}" sh -lc 'cd /app && python3 /app/steward.py reconcile' || true
else
  crontab /etc/cron/crontab
  echo "Crontab installed"

  echo "Running initial reconciliation..."
  sh -lc 'cd /app && python3 /app/steward.py reconcile' || true
fi

# Start cron in foreground, log to stdout
echo "Starting crond..."
exec crond -f -l 6
