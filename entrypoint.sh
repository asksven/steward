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
echo "  Control repo: $(echo "${CONTROL_REPO_URL}" | sed 's|://[^@]*@|://***@|g')"
echo "  Gitops root:  ${GITOPS_ROOT:-/git}"
echo "  Running as:   ${STEWARD_UID}:${STEWARD_GID}"
echo "  Home:         ${STEWARD_HOME}"
echo "  Metrics port: ${METRICS_PORT:-(disabled)}"

# Set up SSH credentials
# Priority:
#   1. STEWARD_CREDENTIALS_FILE (credentials.yml) — multi-key, recommended
#   2. /run/secrets/ssh_key                        — single Docker secret key
STEWARD_CREDENTIALS_FILE="${STEWARD_CREDENTIALS_FILE:-/app/credentials.yml}"

if [ -f "${STEWARD_CREDENTIALS_FILE}" ]; then
  python3 - <<PYEOF
import sys, os
sys.path.insert(0, '/app')
os.environ.setdefault('GITOPS_ROOT', '${GITOPS_ROOT:-/git}')
import steward
try:
    cfg = steward.parse_credentials_file('${STEWARD_CREDENTIALS_FILE}')
    # Validate key files exist
    missing = [e.key_file for e in cfg.credentials if not __import__('os').path.isfile(e.key_file)]
    if missing:
        print(f"WARNING: credentials.yml references missing key files: {', '.join(missing)}", flush=True)
    strict = 'yes' if cfg.known_hosts_file else 'accept-new'
    ssh_config_text = steward.generate_ssh_config(cfg, strict_host_key_checking=strict)
    ssh_dir = '${STEWARD_HOME}/.ssh'
    __import__('os').makedirs(ssh_dir, exist_ok=True)
    with open(f'{ssh_dir}/config', 'w') as f:
        f.write(ssh_config_text)
    __import__('os').chmod(f'{ssh_dir}/config', 0o600)
    __import__('os').chmod(ssh_dir, 0o700)
    print(f"SSH config written for {len(cfg.credentials)} credential entries from {repr('${STEWARD_CREDENTIALS_FILE}')}", flush=True)
except Exception as exc:
    print(f"ERROR: Failed to configure SSH from credentials.yml: {exc}", flush=True)
    sys.exit(1)
PYEOF
  chown -R "${STEWARD_UID}:${STEWARD_GID}" "${STEWARD_HOME}/.ssh"
elif [ -f /run/secrets/ssh_key ]; then
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
elif [ -d /root/.ssh-host ]; then
  echo "ERROR: The /root/.ssh-host bind-mount is no longer supported (removed in steward 0.3.0)."
  echo "  Migrate to credentials.yml: https://github.com/asksven/steward#migrating-from-the-legacy-ssh-host-bind-mount"
  exit 1
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
