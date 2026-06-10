#!/usr/bin/env bash
#
# steward doctor — validate a node's steward setup on the host and inside the
# running container.
#
# Run it from the deployment directory (the one holding .env, credentials.yml
# and docker-compose.override.yml):
#
#   cd /opt/steward && /path/to/steward/scripts/doctor.sh
#
# It runs a series of read-only checks, prints a pass / warn / fail summary, and
# exits non-zero if any check FAILed. It exists to catch the misconfigurations
# that otherwise cost hours of debugging — a wrong host in CONTROL_REPO_URL
# (github.com vs gitlab.com), an empty or mismatched known_hosts, or a missing
# deploy key — and to surface the failure in one clear line.

set -u

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=''; GRN=''; YLW=''; BLU=''; BLD=''; RST=''
fi

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { printf '  %s[PASS]%s %s\n' "$GRN" "$RST" "$1"; PASS_COUNT=$((PASS_COUNT + 1)); }
warn() { printf '  %s[WARN]%s %s\n' "$YLW" "$RST" "$1"; WARN_COUNT=$((WARN_COUNT + 1)); }
fail() { printf '  %s[FAIL]%s %s\n' "$RED" "$RST" "$1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { printf '  %s[INFO]%s %s\n' "$BLU" "$RST" "$1"; }
section() { printf '\n%s== %s ==%s\n' "$BLD" "$1" "$RST"; }

# ---------------------------------------------------------------------------
# .env parsing (read values without sourcing — the file is untrusted input)
# ---------------------------------------------------------------------------

ENV_FILE=".env"

# env_get KEY — print the value of KEY from .env, stripping surrounding quotes.
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^[[:space:]]*${1}=" "$ENV_FILE" 2>/dev/null \
    | tail -n1 \
    | sed -E "s/^[[:space:]]*${1}=//" \
    | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

# extract_host URL — print the hostname from a git remote URL (SSH or HTTPS).
extract_host() {
  case "$1" in
    git@*)            printf '%s' "$1" | sed -E 's/^git@([^:]+):.*/\1/' ;;
    ssh://*)          printf '%s' "$1" | sed -E 's#^ssh://([^/]*@)?([^/:]+).*#\2#' ;;
    http://*|https://*) printf '%s' "$1" | sed -E 's#^https?://([^/@]*@)?([^/:]+).*#\2#' ;;
    *)                printf '' ;;
  esac
}

# redact_url URL — strip user:pass@ credentials from HTTPS URLs for safe display.
redact_url() {
  printf '%s' "$1" | sed -E 's#(https?://)[^/@]*@#\1***@#'
}

# run_timeout SECONDS CMD... — run CMD with a timeout if `timeout` is available.
run_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  else
    "$@"
  fi
}

# ===========================================================================
# Phase A — host checks
# ===========================================================================

section "Phase A — host"

# 1. docker + docker compose v2
if command -v docker >/dev/null 2>&1; then
  pass "docker is installed ($(docker --version 2>/dev/null | head -n1))"
  if docker compose version >/dev/null 2>&1; then
    pass "docker compose v2 is available ($(docker compose version --short 2>/dev/null))"
  else
    fail "docker compose v2 not found (the legacy 'docker-compose' v1 is not supported)"
  fi
else
  fail "docker is not installed or not on PATH"
fi

# 2. .env present + CONTROL_REPO_URL set
CONTROL_REPO_URL=""
if [ -f "$ENV_FILE" ]; then
  pass ".env found in $(pwd)"
  CONTROL_REPO_URL="$(env_get CONTROL_REPO_URL)"
  if [ -n "$CONTROL_REPO_URL" ]; then
    pass "CONTROL_REPO_URL is set"
  else
    fail "CONTROL_REPO_URL is empty or unset in .env"
  fi
  if [ "$CONTROL_REPO_URL" = "git@github.com:you/homelab-gitops.git" ]; then
    warn "CONTROL_REPO_URL is still the .env.example placeholder value"
  fi
else
  fail ".env not found in $(pwd) — run doctor.sh from the deployment directory"
fi

# 3. URL format — FAIL on embedded HTTPS credentials
if [ -n "$CONTROL_REPO_URL" ]; then
  if printf '%s' "$CONTROL_REPO_URL" | grep -qE '^https?://[^/]*@'; then
    fail "CONTROL_REPO_URL embeds credentials in an HTTPS URL — use SSH (git@host:path) for private repos"
  elif printf '%s' "$CONTROL_REPO_URL" | grep -qE '^(git@|ssh://|https?://)'; then
    pass "CONTROL_REPO_URL scheme looks valid"
  else
    fail "CONTROL_REPO_URL is not a recognised SSH or HTTPS URL: $(redact_url "$CONTROL_REPO_URL")"
  fi
fi

# 4. Echo the resolved host (makes github-vs-gitlab typos obvious)
CTRL_HOST=""
if [ -n "$CONTROL_REPO_URL" ]; then
  CTRL_HOST="$(extract_host "$CONTROL_REPO_URL")"
  if [ -n "$CTRL_HOST" ]; then
    info "Control-repo host resolves to: ${BLD}${CTRL_HOST}${RST}"
  else
    warn "Could not extract a host from CONTROL_REPO_URL: $(redact_url "$CONTROL_REPO_URL")"
  fi
fi

# 5. STEWARD_DATA_DIR + UID/GID
DATA_DIR="$(env_get STEWARD_DATA_DIR)"
if [ -n "$DATA_DIR" ]; then
  if [ -d "$DATA_DIR" ]; then
    pass "STEWARD_DATA_DIR exists: $DATA_DIR"
  else
    warn "STEWARD_DATA_DIR does not exist yet (will be created on first run): $DATA_DIR"
  fi
else
  info "STEWARD_DATA_DIR not set — defaults to ./steward-data"
fi

ENV_UID="$(env_get STEWARD_UID)"
ENV_GID="$(env_get STEWARD_GID)"
if [ -z "$ENV_UID" ] || [ "$ENV_UID" = "0" ]; then
  warn "STEWARD_UID is unset or 0 — files in STEWARD_DATA_DIR will be root-owned (set it to 'id -u')"
else
  pass "STEWARD_UID=$ENV_UID, STEWARD_GID=${ENV_GID:-$ENV_UID}"
fi

# 6. credentials.yml (if referenced) parses and its key files are accounted for
OVERRIDE_FILE="docker-compose.override.yml"
CRED_HOST_PATH=""
if [ -f "$OVERRIDE_FILE" ] && grep -q 'credentials.yml' "$OVERRIDE_FILE"; then
  # Pull the host source from a volume line like "- /host/credentials.yml:/app/credentials.yml:ro"
  CRED_HOST_PATH="$(grep -E '[-[:space:]]+[^:]+:/app/credentials\.yml' "$OVERRIDE_FILE" \
    | head -n1 \
    | sed -E 's/^[[:space:]]*-[[:space:]]*//; s#:/app/credentials\.yml.*##' \
    | sed -E 's/^"(.*)"$/\1/')"
fi
[ -z "$CRED_HOST_PATH" ] && [ -f "./credentials.yml" ] && CRED_HOST_PATH="./credentials.yml"

if [ -n "$CRED_HOST_PATH" ]; then
  if [ -f "$CRED_HOST_PATH" ]; then
    pass "credentials.yml found on host: $CRED_HOST_PATH"
    if grep -qE '^[[:space:]]*credentials:' "$CRED_HOST_PATH" && grep -qE '^[[:space:]]*-?[[:space:]]*key_file:' "$CRED_HOST_PATH"; then
      pass "credentials.yml has a 'credentials:' list with key_file entries"
    else
      warn "credentials.yml does not look like a valid steward credentials file (missing 'credentials:' or 'key_file:')"
    fi
    # Check each key_file. Container paths (/run/secrets, /app) are validated inside the container.
    while IFS= read -r kf; do
      [ -z "$kf" ] && continue
      case "$kf" in
        /run/secrets/*|/app/*)
          info "credentials key_file '$kf' is a container path — validated in Phase B" ;;
        /*)
          if [ -s "$kf" ]; then
            pass "credentials key_file exists on host: $kf"
          else
            warn "credentials key_file missing or empty on host: $kf"
          fi ;;
        *)
          info "credentials key_file '$kf' is a relative/container path — validated in Phase B" ;;
      esac
    done < <(grep -E '^[[:space:]]*-?[[:space:]]*key_file:' "$CRED_HOST_PATH" \
      | sed -E 's/^[[:space:]]*-?[[:space:]]*key_file:[[:space:]]*//; s/^"(.*)"$/\1/; s/#.*$//; s/[[:space:]]*$//')
  else
    fail "docker-compose.override.yml references credentials.yml but it is missing: $CRED_HOST_PATH"
  fi
else
  info "No credentials.yml referenced — assuming Docker-secret single-key SSH setup"
fi

# 7. known_hosts host entry (host-side, if referenced in credentials.yml)
if [ -n "$CRED_HOST_PATH" ] && [ -f "$CRED_HOST_PATH" ]; then
  KH_REF="$(grep -E '^[[:space:]]*known_hosts_file:' "$CRED_HOST_PATH" \
    | head -n1 \
    | sed -E 's/^[[:space:]]*known_hosts_file:[[:space:]]*//; s/^"(.*)"$/\1/; s/#.*$//; s/[[:space:]]*$//')"
  if [ -n "$KH_REF" ]; then
    info "credentials.yml references known_hosts_file: $KH_REF (forces StrictHostKeyChecking=yes; verified in Phase B)"
  else
    info "No known_hosts_file in credentials.yml — host-key checking uses accept-new"
  fi
fi

# ===========================================================================
# Phase B — container checks
# ===========================================================================

section "Phase B — container"

CONTAINER="$(env_get AGENT_CONTAINER_NAME)"
[ -z "$CONTAINER" ] && CONTAINER="steward"

CONTAINER_RUNNING=0
if command -v docker >/dev/null 2>&1 \
  && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  CONTAINER_RUNNING=1
fi

if [ "$CONTAINER_RUNNING" -ne 1 ]; then
  warn "Container '$CONTAINER' is not running — skipping in-container checks. Start it with 'docker compose up -d'."
else
  # 8. Container running
  pass "Container '$CONTAINER' is running"

  # Helpers scoped to the running container.
  cexec() { docker exec "$CONTAINER" "$@"; }

  # 9. Detect effective HOME (steward runs as non-root with HOME=/home/steward;
  #    docker exec defaults to root where ~ is /root).
  C_UID="$(cexec printenv STEWARD_UID 2>/dev/null)"; C_UID="${C_UID:-0}"
  C_GID="$(cexec printenv STEWARD_GID 2>/dev/null)"; C_GID="${C_GID:-$C_UID}"
  if [ "$C_UID" != "0" ]; then
    C_HOME="/home/steward"
  else
    C_HOME="/root"
  fi
  # Fall back to whichever HOME actually has an SSH config.
  if ! cexec test -f "$C_HOME/.ssh/config" 2>/dev/null; then
    for h in /home/steward /root; do
      if cexec test -f "$h/.ssh/config" 2>/dev/null; then C_HOME="$h"; break; fi
    done
  fi
  info "Effective container HOME: $C_HOME (running as ${C_UID}:${C_GID})"

  SSH_CONFIG_PATH="$C_HOME/.ssh/config"
  SSH_CONFIG=""
  if cexec test -f "$SSH_CONFIG_PATH" 2>/dev/null; then
    SSH_CONFIG="$(cexec cat "$SSH_CONFIG_PATH" 2>/dev/null)"
    pass "SSH config present at $SSH_CONFIG_PATH"
  else
    fail "No SSH config at $SSH_CONFIG_PATH — credentials.yml or Docker secret not configured"
  fi

  # 10/11/12. Resolve the config that applies to the control host.
  IDF=""; UKHF=""; STRICT=""
  if [ -n "$SSH_CONFIG" ] && [ -n "$CTRL_HOST" ]; then
    eval "$(printf '%s\n' "$SSH_CONFIG" | awk -v h="$CTRL_HOST" '
      /^[Hh]ost /{ inblock=0; for (i=2;i<=NF;i++) if ($i==h || $i=="*") inblock=1; next }
      !inblock { next }
      tolower($1)=="identityfile"        { idf=$2 }
      tolower($1)=="userknownhostsfile"  { ukhf=$2 }
      tolower($1)=="stricthostkeychecking"{ strict=$2 }
      END {
        printf "IDF=%s\nUKHF=%s\nSTRICT=%s\n", idf, ukhf, strict
      }')"
    info "SSH config for ${CTRL_HOST}: StrictHostKeyChecking=${STRICT:-default} UserKnownHostsFile=${UKHF:-<none>} IdentityFile=${IDF:-<none>}"
  fi

  # 12. Identity key file exists and is non-empty inside the container.
  if [ -n "$IDF" ]; then
    if cexec test -s "$IDF" 2>/dev/null; then
      pass "Identity key file exists and is non-empty: $IDF"
    else
      fail "Identity key file missing or empty in container: $IDF"
    fi
  else
    warn "Could not determine the IdentityFile for $CTRL_HOST from the SSH config"
  fi

  # 11. known_hosts: resolve the file SSH actually uses and verify the host key.
  if [ -n "$UKHF" ]; then
    if cexec test -s "$UKHF" 2>/dev/null; then
      if cexec ssh-keygen -F "$CTRL_HOST" -f "$UKHF" >/dev/null 2>&1; then
        pass "known_hosts ($UKHF) contains an entry for $CTRL_HOST"
      else
        if [ "$STRICT" = "yes" ]; then
          fail "known_hosts ($UKHF) has no entry for $CTRL_HOST and StrictHostKeyChecking=yes — every clone will fail with 'Host key verification failed'"
        else
          warn "known_hosts ($UKHF) has no entry for $CTRL_HOST (accept-new will add it on first connect)"
        fi
      fi
    else
      if [ "$STRICT" = "yes" ]; then
        fail "UserKnownHostsFile is empty or missing ($UKHF) and StrictHostKeyChecking=yes — clones will fail"
      else
        warn "UserKnownHostsFile is empty or missing ($UKHF), but accept-new is in effect"
      fi
    fi
  else
    info "No UserKnownHostsFile configured — host-key checking relies on accept-new"
  fi

  # 13. Live test: git ls-remote reproduces the real clone path. Run as the
  #     steward user with the correct HOME — mirrors how the reconcile cron job
  #     actually invokes git/ssh. docker exec is run directly (not via a shell
  #     function) so `timeout` can exec it.
  if [ -n "$CONTROL_REPO_URL" ]; then
    LSREMOTE_OUT="$(run_timeout 30 docker exec -u "${C_UID}:${C_GID}" -e HOME="$C_HOME" \
      -e GIT_TERMINAL_PROMPT=0 "$CONTAINER" git ls-remote "$CONTROL_REPO_URL" 2>&1)"
    LSREMOTE_RC=$?
    if [ "$LSREMOTE_RC" -eq 0 ]; then
      pass "git ls-remote succeeded — the control repo is reachable with the configured credentials"
    elif [ "$LSREMOTE_RC" -eq 124 ]; then
      fail "git ls-remote timed out after 30s against $(redact_url "$CONTROL_REPO_URL") (network or DNS issue?)"
    else
      fail "git ls-remote failed (exit $LSREMOTE_RC) against $(redact_url "$CONTROL_REPO_URL"):"
      printf '%s\n' "$LSREMOTE_OUT" | sed 's/^/         /'
    fi
  fi

  # 14. Drift: container env vs .env on disk.
  C_CTRL_URL="$(cexec printenv CONTROL_REPO_URL 2>/dev/null)"
  if [ -n "$C_CTRL_URL" ] && [ -n "$CONTROL_REPO_URL" ]; then
    if [ "$C_CTRL_URL" = "$CONTROL_REPO_URL" ]; then
      pass "Container CONTROL_REPO_URL matches .env"
    else
      warn "Drift: container has '$(redact_url "$C_CTRL_URL")' but .env has '$(redact_url "$CONTROL_REPO_URL")' — recreate the container ('docker compose up -d') to apply .env changes"
    fi
  fi
fi

# ===========================================================================
# Summary
# ===========================================================================

section "Summary"
printf '  %s%d passed%s / %s%d warnings%s / %s%d failed%s\n' \
  "$GRN" "$PASS_COUNT" "$RST" \
  "$YLW" "$WARN_COUNT" "$RST" \
  "$RED" "$FAIL_COUNT" "$RST"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
