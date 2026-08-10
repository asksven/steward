#!/usr/bin/env bash
#
# steward setup — interactively generate a node's deploy key and config files
# (.env, credentials.yml, docker-compose.override.yml) with follow-up instructions.
#
# Run it from the cloned steward repo:
#
#   ./scripts/setup.sh
#
# It generates a dedicated read-only SSH deploy key, pins the control host's key
# after you verify its fingerprint, and writes config next to steward's own
# managed clone at <STEWARD_DATA_DIR>/stacks/steward (see README "Bootstrap") so
# self-update does not lose it. It never overwrites existing keys, and asks
# before overwriting existing config files.

set -u -o pipefail
umask 077  # keys and config files are created private (0600) by default

# ---------------------------------------------------------------------------
# Output helpers (match scripts/doctor.sh)
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=''; GRN=''; YLW=''; BLU=''; BLD=''; RST=''
fi

ok()   { printf '  %s[OK]%s   %s\n' "$GRN" "$RST" "$1"; }
warn() { printf '  %s[WARN]%s %s\n' "$YLW" "$RST" "$1"; }
err()  { printf '  %s[FAIL]%s %s\n' "$RED" "$RST" "$1" >&2; }
info() { printf '  %s[INFO]%s %s\n' "$BLU" "$RST" "$1"; }
step() { printf '\n%s== %s ==%s\n' "$BLD" "$1" "$RST"; }

# ---------------------------------------------------------------------------
# Prompt helpers — all input is treated as untrusted (quoted, never eval'd)
# ---------------------------------------------------------------------------

# prompt_required VAR "question" — re-ask until a non-empty value is given.
prompt_required() {
  local __var="$1" __q="$2" __ans=""
  while :; do
    printf '  %s: ' "$__q" >&2
    IFS= read -r __ans || true
    [ -n "$__ans" ] && break
    warn "a value is required"
  done
  printf -v "$__var" '%s' "$__ans"
}

# prompt_default VAR "question" "default" — use default when input is empty.
prompt_default() {
  local __var="$1" __q="$2" __def="$3" __ans=""
  printf '  %s [%s]: ' "$__q" "$__def" >&2
  IFS= read -r __ans || true
  [ -z "$__ans" ] && __ans="$__def"
  printf -v "$__var" '%s' "$__ans"
}

# confirm "question" — returns 0 for yes, 1 otherwise (default no).
confirm() {
  local __ans=""
  printf '  %s [y/N]: ' "$1" >&2
  IFS= read -r __ans || true
  case "$__ans" in
    [yY] | [yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# extract_host URL — print the hostname from a git remote URL (SSH or HTTPS).
extract_host() {
  case "$1" in
    git@*)              printf '%s' "$1" | sed -E 's/^git@([^:]+):.*/\1/' ;;
    ssh://*)            printf '%s' "$1" | sed -E 's#^ssh://([^/]*@)?([^/:]+).*#\2#' ;;
    http://* | https://*) printf '%s' "$1" | sed -E 's#^https?://([^/@]*@)?([^/:]+).*#\2#' ;;
    *)                  printf '' ;;
  esac
}

# maybe_overwrite PATH — 0 if we should (over)write PATH, 1 to keep existing.
maybe_overwrite() {
  [ -e "$1" ] || return 0
  confirm "$1 exists — overwrite?" && return 0
  warn "kept existing $1"
  return 1
}

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
#
# steward manages its own updates by re-cloning itself into
# <STEWARD_DATA_DIR>/stacks/steward (see README "Bootstrap") and only loading
# .env / credentials.yml / docker-compose.override.yml that live beside THAT
# clone. If this script isn't run from that location, config written here
# would be silently lost on the first self-update — so detect the mismatch
# and provision the right layout instead of guessing.

printf '%ssteward setup%s\n' "$BLD" "$RST"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLONE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CLONE_PARENT="$(dirname -- "$CLONE_DIR")"

if [ "$(basename -- "$CLONE_DIR")" = "steward" ] && [ "$(basename -- "$CLONE_PARENT")" = "stacks" ]; then
  DATA_DIR="$(dirname -- "$CLONE_PARENT")"
else
  step "Deployment layout"
  warn "This clone (${CLONE_DIR}) is not at <STEWARD_DATA_DIR>/stacks/steward."
  warn "steward re-clones itself there on self-update; config written anywhere"
  warn "else is lost the first time that happens."

  prompt_default DATA_DIR "STEWARD_DATA_DIR to use" "${HOME}/steward-data"
  TARGET_CLONE_DIR="${DATA_DIR}/stacks/steward"
  info "steward's managed clone will live at: ${TARGET_CLONE_DIR}"

  if [ -e "$TARGET_CLONE_DIR" ]; then
    err "${TARGET_CLONE_DIR} already exists — inspect it, then re-run ./scripts/setup.sh from inside it."
    exit 1
  fi

  if ! confirm "Provision steward at ${TARGET_CLONE_DIR} and continue setup from there?"; then
    info "Nothing written. Clone steward into ${TARGET_CLONE_DIR} yourself, then run:"
    info "  ${TARGET_CLONE_DIR}/scripts/setup.sh"
    exit 1
  fi

  mkdir -p "$(dirname -- "$TARGET_CLONE_DIR")" || { err "failed to create $(dirname -- "$TARGET_CLONE_DIR")"; exit 1; }

  ORIGIN_URL="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || true)"
  if [ -n "$ORIGIN_URL" ]; then
    git clone "$ORIGIN_URL" "$TARGET_CLONE_DIR" || { err "git clone of ${ORIGIN_URL} failed"; exit 1; }
  else
    warn "no git remote 'origin' found on ${CLONE_DIR} — copying the working tree instead"
    cp -a "$CLONE_DIR" "$TARGET_CLONE_DIR" || { err "copy to ${TARGET_CLONE_DIR} failed"; exit 1; }
  fi
  ok "provisioned ${TARGET_CLONE_DIR}"

  exec "${TARGET_CLONE_DIR}/scripts/setup.sh"
fi

ENV_FILE="${CLONE_DIR}/.env"
CRED_FILE="${CLONE_DIR}/credentials.yml"
OVERRIDE_FILE="${CLONE_DIR}/docker-compose.override.yml"

SSH_DIR="${HOME}/.ssh"
KEY_FILE="${SSH_DIR}/steward_deploy_key"
KNOWN_HOSTS_FILE="${SSH_DIR}/steward_known_hosts"

info "Deployment directory: ${CLONE_DIR}"
info "STEWARD_DATA_DIR:     ${DATA_DIR}"

# ---------------------------------------------------------------------------
# 1. Control repo + node identity
# ---------------------------------------------------------------------------

step "Control repo"

prompt_required CONTROL_REPO_URL "Control repo URL (git@host:path or ssh://host/path)"

# Reject HTTPS URLs with embedded credentials (same rule steward enforces).
if printf '%s' "$CONTROL_REPO_URL" | grep -qE '^https?://[^/]*@'; then
  err "HTTPS URLs with embedded credentials are not supported — use SSH (git@host:path)."
  exit 1
fi
case "$CONTROL_REPO_URL" in
  git@* | ssh://* | https://* | http://*) : ;;
  *) err "Not a recognised SSH or HTTPS URL: ${CONTROL_REPO_URL}"; exit 1 ;;
esac

CTRL_HOST="$(extract_host "$CONTROL_REPO_URL")"
if [ -z "$CTRL_HOST" ]; then
  err "Could not extract a host from: ${CONTROL_REPO_URL}"
  exit 1
fi
info "Control-repo host: ${BLD}${CTRL_HOST}${RST}"

prompt_default CONTROL_REPO_BRANCH "Control repo branch" "main"
prompt_default GITOPS_NODE_NAME "Node name (directory under nodes/ in the control repo)" "$(hostname)"

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

# ---------------------------------------------------------------------------
# 2. Deploy key (dedicated, read-only, passphrase-less for the container)
# ---------------------------------------------------------------------------

step "Deploy key"

mkdir -p "$SSH_DIR" || { err "failed to create ${SSH_DIR}"; exit 1; }
chmod 700 "$SSH_DIR" || { err "failed to chmod ${SSH_DIR}"; exit 1; }

if [ -f "$KEY_FILE" ]; then
  info "Reusing existing key (not overwritten): ${KEY_FILE}"
  if [ ! -f "${KEY_FILE}.pub" ]; then
    warn "${KEY_FILE}.pub is missing — deriving it from the private key"
    ssh-keygen -y -f "$KEY_FILE" > "${KEY_FILE}.pub" || { err "failed to derive ${KEY_FILE}.pub"; exit 1; }
  fi
else
  ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "steward@${GITOPS_NODE_NAME}" >/dev/null || { err "ssh-keygen failed"; exit 1; }
  ok "generated ${KEY_FILE}"
fi
chmod 600 "$KEY_FILE" || { err "failed to chmod ${KEY_FILE}"; exit 1; }
chmod 644 "${KEY_FILE}.pub" || { err "failed to chmod ${KEY_FILE}.pub"; exit 1; }

# ---------------------------------------------------------------------------
# 3. Host-key trust — scan, then require fingerprint verification
# ---------------------------------------------------------------------------

step "Host-key trust (strict)"

SCAN="$(ssh-keyscan -t rsa,ecdsa,ed25519 "$CTRL_HOST" 2>/dev/null)"
if [ -z "$SCAN" ]; then
  err "ssh-keyscan returned nothing for ${CTRL_HOST} (network or DNS problem?)."
  exit 1
fi

FP_TMP="$(mktemp "${TMPDIR:-/tmp}/steward-hostkey.XXXXXX")"
printf '%s\n' "$SCAN" > "$FP_TMP"

warn "ssh-keyscan trusts whatever the network returned right now — it does NOT verify"
warn "the key. Compare the fingerprints below against the provider's PUBLISHED list:"
warn "  GitHub: https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints"
warn "  GitLab: https://docs.gitlab.com/ee/user/gitlab_com/#ssh-host-keys-fingerprints"
echo
ssh-keygen -l -f "$FP_TMP" | sed 's/^/    /'
rm -f "$FP_TMP"
echo

if ! confirm "Do these fingerprints match the provider's published fingerprints?"; then
  err "Aborted without writing known_hosts. Re-run once you have verified them."
  exit 1
fi

# Drop any previously trusted key for this host before adding the freshly
# verified one, so a stale or rotated-away key is never left trusted.
if [ -f "$KNOWN_HOSTS_FILE" ]; then
  ssh-keygen -R "$CTRL_HOST" -f "$KNOWN_HOSTS_FILE" >/dev/null 2>&1 || true
  rm -f "${KNOWN_HOSTS_FILE}.old"
fi

printf '%s\n' "$SCAN" >> "$KNOWN_HOSTS_FILE" || { err "failed to write ${KNOWN_HOSTS_FILE}"; exit 1; }
sort -u "$KNOWN_HOSTS_FILE" -o "$KNOWN_HOSTS_FILE" || { err "failed to dedupe ${KNOWN_HOSTS_FILE}"; exit 1; }
chmod 644 "$KNOWN_HOSTS_FILE" || { err "failed to chmod ${KNOWN_HOSTS_FILE}"; exit 1; }
ok "wrote ${KNOWN_HOSTS_FILE}"

# ---------------------------------------------------------------------------
# 4. Config files
# ---------------------------------------------------------------------------

step "Config files"

mkdir -p "$DATA_DIR" || { err "failed to create ${DATA_DIR}"; exit 1; }

if maybe_overwrite "$ENV_FILE"; then
  cat > "$ENV_FILE" <<EOF
CONTROL_REPO_URL=${CONTROL_REPO_URL}
CONTROL_REPO_BRANCH=${CONTROL_REPO_BRANCH}
GITOPS_NODE_NAME=${GITOPS_NODE_NAME}
STEWARD_DATA_DIR=${DATA_DIR}
STEWARD_UID=${HOST_UID}
STEWARD_GID=${HOST_GID}
EOF
  chmod 600 "$ENV_FILE" || { err "failed to chmod ${ENV_FILE}"; exit 1; }
  ok "wrote ${ENV_FILE}"
fi

if maybe_overwrite "$CRED_FILE"; then
  cat > "$CRED_FILE" <<EOF
credentials:
  - pattern: ${CTRL_HOST}
    key_file: /run/secrets/control_key
known_hosts_file: /run/secrets/ssh_known_hosts
EOF
  chmod 600 "$CRED_FILE" || { err "failed to chmod ${CRED_FILE}"; exit 1; }
  ok "wrote ${CRED_FILE}"
fi

if maybe_overwrite "$OVERRIDE_FILE"; then
  cat > "$OVERRIDE_FILE" <<EOF
services:
  steward:
    volumes:
      - ./credentials.yml:/app/credentials.yml:ro
    secrets:
      - control_key
      - ssh_known_hosts

secrets:
  control_key:
    file: ${KEY_FILE}
  ssh_known_hosts:
    file: ${KNOWN_HOSTS_FILE}
EOF
  chmod 600 "$OVERRIDE_FILE" || { err "failed to chmod ${OVERRIDE_FILE}"; exit 1; }
  ok "wrote ${OVERRIDE_FILE}"
fi

# ---------------------------------------------------------------------------
# 5. Next steps
# ---------------------------------------------------------------------------

step "Next steps"

if [ -f "${KEY_FILE}.pub" ]; then
  cat <<EOF
  1. Add this PUBLIC key as a READ-ONLY deploy key on the control repo
     (${CONTROL_REPO_URL}):

$(sed 's/^/       /' "${KEY_FILE}.pub")

  2. Start steward:   (cd "${CLONE_DIR}" && docker compose up -d)
  3. Validate setup:  ${SCRIPT_DIR}/doctor.sh
EOF
else
  err "expected ${KEY_FILE}.pub to exist but it does not — check ${KEY_FILE} manually."
fi

cat <<EOF

  Adding another GitHub stack repo — GitHub requires a separate key per repo:
    a. ssh-keygen -t ed25519 -f ~/.ssh/steward_<name>_key -N ""
    b. Add the .pub as a read-only deploy key on that repo.
    c. In docker-compose.override.yml add a secret:
         <name>_key:
           file: ${SSH_DIR}/steward_<name>_key
       and list "- <name>_key" under services.steward.secrets.
    d. In credentials.yml add an aliased entry:
         - pattern: ${CTRL_HOST}-<name>
           hostname: ${CTRL_HOST}
           key_file: /run/secrets/<name>_key
    e. Use the alias in the app manifest's repo URL:
         repo: git@${CTRL_HOST}-<name>:you/<name>.git
EOF

echo
if confirm "Run ${SCRIPT_DIR}/doctor.sh now?"; then
  "${SCRIPT_DIR}/doctor.sh" || true
fi
