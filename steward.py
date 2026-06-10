#!/usr/bin/env python3
"""
steward
Watches a control repo for app manifests and reconciles docker compose stacks.
"""

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib import error, request
from urllib.parse import urlparse, urlunparse

import yaml
from git import GitCommandError, InvalidGitRepositoryError, Repo

from state_store import load_state as load_sqlite_state
from state_store import save_state as save_sqlite_state

_EMBEDDED_CREDENTIALS_RE = re.compile(r"(https?://)([^:@\s]+:[^@\s]+@)")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_level = getattr(logging, os.environ.get("LOGLEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("steward")

# ---------------------------------------------------------------------------
# Constants (read once at import time)
# ---------------------------------------------------------------------------

AGENT_CONTAINER_NAME = os.environ.get("AGENT_CONTAINER_NAME", "steward")

# ---------------------------------------------------------------------------
# Path helpers (inside ↔ outside)
# ---------------------------------------------------------------------------


def _container_mounts() -> list[dict]:
    """Return the Mounts list from docker inspect on this container, or []."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", AGENT_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) or []
    except Exception:
        pass
    return []


def _find_best_mount(container_path: Path) -> tuple[dict, str]:
    """Return (best_mount_dict, rel_path) for the mount covering container_path."""
    mounts = _container_mounts()
    best: dict = {}
    best_len = 0
    for mount in mounts:
        dest = mount.get("Destination", "")
        if str(container_path).startswith(dest) and len(dest) > best_len:
            best = mount
            best_len = len(dest)
    rel = str(container_path)[best_len:].lstrip("/") if best_len else ""
    return best, rel


def host_path(container_path: Path) -> str:
    """
    Resolve a container path to its host-side source by inspecting mounts.
    Returns a human-readable string; never raises.
    """
    best, rel = _find_best_mount(container_path)
    if not best:
        return "<host path unknown>"
    source = best.get("Source", "")
    name = best.get("Name", "")
    outside = source + ("/" + rel if rel else "")
    if name:
        return f"{outside}  [volume: {name}]"
    return outside


def _resolve_host_path(container_path: Path) -> Optional[str]:
    """
    Resolve a container path to its host filesystem path.
    Returns None if the path is not covered by any mount or docker inspect failed.
    Used for constructing bind-mount arguments for peer containers.
    """
    best, rel = _find_best_mount(container_path)
    if not best:
        return None
    source = best.get("Source", "")
    return source + ("/" + rel if rel else "")


def log_mounts() -> None:
    """Log all container mounts at DEBUG level."""
    mounts = _container_mounts()
    if not mounts:
        log.debug("Mount map unavailable (docker inspect '%s' failed)", AGENT_CONTAINER_NAME)
        return
    log.debug("Container mount map:")
    for m in mounts:
        mtype = m.get("Type", "?")
        src = m.get("Source") or m.get("Name", "?")
        dst = m.get("Destination", "?")
        mode = m.get("Mode", "")
        log.debug("  [%s] %s → %s  (%s)", mtype, src, dst, mode)


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


def strip_url_credentials(url: str) -> str:
    """Remove embedded credentials from a URL or any string containing HTTPS URLs.

    Handles both pure URLs (e.g. metric labels) and arbitrary strings such as
    GitCommandError messages where credentials may appear inside a longer text.
    """
    try:
        p = urlparse(url)
        if p.scheme in ("http", "https") and p.username:
            netloc = p.hostname + (f":{p.port}" if p.port else "")
            return urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass
    # Fallback: strip user:pass@ from any HTTPS URL embedded in a longer string
    return _EMBEDDED_CREDENTIALS_RE.sub(r"\1", url)


def is_ssh_url(url: str) -> bool:
    """Return True if the URL is an SSH git URL (git@host:... or ssh://)."""
    if url.startswith("git@") or url.startswith("ssh://"):
        return True
    try:
        p = urlparse(url)
        if p.scheme == "ssh":
            return True
    except Exception:
        pass
    return False


def validate_repo_url(url: str, context: str = "repo") -> Optional[str]:
    """Validate a repo URL. SSH and plain HTTPS are accepted; HTTPS with embedded credentials is not."""
    if not url:
        return f"{context}: URL is empty"
    if is_ssh_url(url):
        return None
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        if parsed.username or parsed.password:
            return (
                f"{context}: HTTPS URLs with embedded credentials are not supported. "
                f"Use SSH (git@host:path) for private repos, or plain HTTPS for public repos."
            )
        return None
    return (
        f"{context}: unsupported URL scheme '{parsed.scheme}'. "
        f"Use SSH (git@host:path or ssh://host/path) or plain HTTPS for public repos."
    )


def _load_metrics_state() -> dict:
    return load_sqlite_state(DB_FILE, GITOPS_NODE_NAME)


def _save_metrics_state(state: dict) -> None:
    try:
        save_sqlite_state(DB_FILE, GITOPS_NODE_NAME, state)
    except Exception as e:
        log.warning("Failed to save metrics state: %s", e)


def _maybe_warn_legacy_json_state() -> None:
    if not LEGACY_METRICS_STATE_FILE.exists():
        return
    if LEGACY_NOTICE_MARKER_FILE.exists():
        return

    log.info(
        "Legacy metrics state file detected at %s; SQLite state at %s is authoritative and starts fresh by design",
        LEGACY_METRICS_STATE_FILE,
        DB_FILE,
    )
    try:
        LEGACY_NOTICE_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_NOTICE_MARKER_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except Exception:
        pass


def _inc(d: dict, *keys, by: int = 1) -> None:
    """Increment a nested counter in a state dict."""
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = d.get(keys[-1], 0) + by


# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

GITOPS_ROOT = Path(os.environ.get("GITOPS_ROOT", Path.home() / "git"))
GITOPS_NODE_NAME = os.environ.get("GITOPS_NODE_NAME", socket.gethostname())
CONTROL_REPO_URL = os.environ.get("CONTROL_REPO_URL", "")
CONTROL_REPO_BRANCH = os.environ.get("CONTROL_REPO_BRANCH", "main")
CONTROL_REPO_DIR = GITOPS_ROOT / "control"
STACKS_DIR = GITOPS_ROOT / "stacks"
DB_FILE = GITOPS_ROOT / "steward.db"
STEWARD_NOTIFY_URL = os.environ.get("STEWARD_NOTIFY_URL", "")
STEWARD_DRY_RUN = os.environ.get("STEWARD_DRY_RUN", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LEGACY_METRICS_STATE_FILE = GITOPS_ROOT / "metrics" / "state.json"
LEGACY_NOTICE_MARKER_FILE = GITOPS_ROOT / "metrics" / ".legacy_json_ignored"

# ---------------------------------------------------------------------------
# App manifest schema
# ---------------------------------------------------------------------------

SUPPORTED_VERSIONS = {1, 2}
SUPPORTED_SYNC_POLICIES = {"auto", "manual"}
SUPPORTED_PULL_POLICIES = {"always", "missing", "never"}

# ---------------------------------------------------------------------------
# Credential configuration (credentials.yml)
# ---------------------------------------------------------------------------

STEWARD_CREDENTIALS_FILE = os.environ.get("STEWARD_CREDENTIALS_FILE", "/app/credentials.yml")


@dataclass
class CredentialEntry:
    pattern: str  # glob pattern matched against the git host (e.g. "github.com", "*.internal")
    key_file: str  # path to the SSH private key file (e.g. /run/secrets/github_key)


@dataclass
class CredentialsConfig:
    credentials: list[CredentialEntry]
    known_hosts_file: Optional[str] = None  # optional path to a known_hosts file


def parse_credentials_file(path: str) -> CredentialsConfig:
    """Parse and validate a credentials.yml file.

    Returns a CredentialsConfig. Raises ValueError on schema problems.
    Key files are not required to exist at parse time so this is testable without
    real files on disk; entrypoint logic validates existence separately.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("credentials.yml must be a YAML mapping")

    entries_raw = raw.get("credentials")
    if not isinstance(entries_raw, list):
        raise ValueError("credentials.yml must have a 'credentials' list")

    entries: list[CredentialEntry] = []
    for i, item in enumerate(entries_raw):
        if not isinstance(item, dict):
            raise ValueError(f"credentials[{i}] must be a mapping")
        pattern = item.get("pattern")
        key_file = item.get("key_file")
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"credentials[{i}]: 'pattern' is required and must be a string")
        if not key_file or not isinstance(key_file, str):
            raise ValueError(f"credentials[{i}]: 'key_file' is required and must be a string")
        # Warn about path-component patterns — SSH Host matching is hostname-only
        if "/" in pattern and pattern != "*":
            log.warning(
                "credentials[%d]: pattern '%s' contains path components; "
                "SSH Host matching uses hostname only — consider using just '%s'",
                i,
                pattern,
                pattern.split("/")[0],
            )
        entries.append(CredentialEntry(pattern=pattern, key_file=key_file))

    known_hosts_file = raw.get("known_hosts_file")
    if known_hosts_file is not None and not isinstance(known_hosts_file, str):
        raise ValueError("credentials.yml: 'known_hosts_file' must be a string path")

    return CredentialsConfig(credentials=entries, known_hosts_file=known_hosts_file or None)


def generate_ssh_config(
    config: CredentialsConfig, strict_host_key_checking: str = "accept-new"
) -> str:
    """Generate the text content of an ~/.ssh/config file from a CredentialsConfig.

    Each credential entry produces one Host block. A trailing global Host * block
    sets UserKnownHostsFile if known_hosts_file is provided.
    """
    lines: list[str] = []
    for entry in config.credentials:
        # Extract hostname from pattern for the SSH Host directive.
        # Path-component parts (e.g. "github.com/org") are dropped; Host matching
        # operates on the hostname only. A warning is already emitted at parse time.
        host_part = entry.pattern.split("/")[0]
        lines.append(f"Host {host_part}")
        lines.append(f"  IdentityFile {entry.key_file}")
        lines.append("  IdentitiesOnly yes")
        lines.append(f"  StrictHostKeyChecking {strict_host_key_checking}")
        lines.append("")

    # Global fallback block
    lines.append("Host *")
    if config.known_hosts_file:
        lines.append(f"  UserKnownHostsFile {config.known_hosts_file}")
    lines.append(f"  StrictHostKeyChecking {strict_host_key_checking}")
    lines.append("")

    return "\n".join(lines)


@dataclass
class AppRef:
    branch: Optional[str] = None
    tag: Optional[str] = None


@dataclass
class AppManifest:
    version: int
    name: str
    repo: str
    ref: AppRef
    path: str
    compose_file: str
    env_file: Optional[str]
    enabled: bool
    source_file: Path
    sync_policy: str = "auto"
    pull_policy: str = "always"
    health_check_delay_seconds: int = 30
    notify_url: Optional[str] = None


class SyncStatus(Enum):
    SYNCED = "Synced"
    OUT_OF_SYNC = "OutOfSync"
    UNKNOWN = "Unknown"


SYNC_STATUS_DISABLED = "Disabled"
HEALTH_STATUS_HEALTHY = "Healthy"
HEALTH_STATUS_DEGRADED = "Degraded"
HEALTH_STATUS_PROGRESSING = "Progressing"
HEALTH_STATUS_UNKNOWN = "Unknown"


@dataclass
class CheckResult:
    status: SyncStatus
    local_sha: Optional[str] = None
    remote_sha: Optional[str] = None


@dataclass
class SyncResult:
    success: bool
    message: str = ""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_operation(
    state: dict,
    *,
    app: AppManifest,
    trigger: str,
    from_sha: Optional[str],
    to_sha: Optional[str],
    sync_status: str,
    message: str,
    health_status: Optional[str],
    duration_s: Optional[float] = None,
) -> None:
    now_iso = _now_iso()
    state.setdefault("_operations", []).append(
        {
            "app": app.name,
            "node": GITOPS_NODE_NAME,
            "started_at": now_iso,
            "completed_at": now_iso,
            "trigger": trigger,
            "from_sha": from_sha,
            "to_sha": to_sha or "",
            "sync_status": sync_status,
            "health_status": health_status,
            "duration_s": duration_s,
            "message": message,
        }
    )


def _notification_target(app: AppManifest) -> Optional[str]:
    return app.notify_url or STEWARD_NOTIFY_URL or None


def _send_notification(app: AppManifest, event: str, payload: dict) -> None:
    target = _notification_target(app)
    if not target:
        return

    body = json.dumps(
        {
            "event": event,
            "node": GITOPS_NODE_NAME,
            "app": app.name,
            "timestamp": _now_iso(),
            **payload,
        }
    ).encode("utf-8")

    req = request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                log.warning(
                    "Notification endpoint returned HTTP %s for app '%s'", resp.status, app.name
                )
    except (error.URLError, TimeoutError, ValueError) as e:
        log.warning("Notification send failed for app '%s': %s", app.name, e)


def parse_manifest(manifest_path: Path) -> AppManifest:
    """Parse and validate an app manifest YAML file."""
    with open(manifest_path) as f:
        raw = yaml.safe_load(f)

    errors = []

    # version
    version = raw.get("version")
    if version is None:
        errors.append("missing required field: version")
    elif version not in SUPPORTED_VERSIONS:
        errors.append(f"unsupported version: {version} (supported: {SUPPORTED_VERSIONS})")

    # name
    name = raw.get("name")
    if not name:
        errors.append("missing required field: name")

    # repo
    repo = raw.get("repo")
    if not repo:
        errors.append("missing required field: repo")
    else:
        url_err = validate_repo_url(repo, context="repo")
        if url_err:
            errors.append(url_err)

    # ref
    ref_raw = raw.get("ref")
    if not ref_raw:
        errors.append("missing required field: ref (must specify branch or tag)")
    else:
        branch = ref_raw.get("branch")
        tag = ref_raw.get("tag")
        if branch and tag:
            errors.append("ref must specify either branch or tag, not both")
        elif not branch and not tag:
            errors.append("ref must specify either branch or tag")
        ref = AppRef(branch=branch, tag=tag)

    # enabled — hard error if missing
    enabled = raw.get("enabled")
    if enabled is None:
        errors.append("missing required field: enabled")

    compose_env_file = raw.get("compose_env_file")
    legacy_env_file = raw.get("env_file")

    if compose_env_file and legacy_env_file:
        errors.append("compose_env_file and env_file are mutually exclusive")

    sync_policy = (raw.get("sync_policy") or "auto").lower()
    if sync_policy not in SUPPORTED_SYNC_POLICIES:
        errors.append(
            f"unsupported sync_policy: {sync_policy} (supported: {sorted(SUPPORTED_SYNC_POLICIES)})"
        )

    pull_policy = (raw.get("pull_policy") or "always").lower()
    if pull_policy not in SUPPORTED_PULL_POLICIES:
        errors.append(
            f"unsupported pull_policy: {pull_policy} (supported: {sorted(SUPPORTED_PULL_POLICIES)})"
        )

    health_delay_raw = raw.get("health_check_delay_seconds", 30)
    try:
        health_check_delay_seconds = int(health_delay_raw)
    except (TypeError, ValueError):
        errors.append("health_check_delay_seconds must be an integer")
        health_check_delay_seconds = 30

    if health_check_delay_seconds < 0:
        errors.append("health_check_delay_seconds must be >= 0")
    elif health_check_delay_seconds > 600:
        errors.append("health_check_delay_seconds must be <= 600")

    notify_url = raw.get("notify_url") or None

    if errors:
        raise ValueError(f"Invalid manifest {manifest_path}: {'; '.join(errors)}")

    if legacy_env_file:
        log.warning(
            "Manifest %s uses deprecated field 'env_file'; migrate to 'compose_env_file'",
            manifest_path.name,
        )

    env_file = compose_env_file or legacy_env_file or None

    return AppManifest(
        version=version,
        name=name,
        repo=repo,
        ref=ref,
        path=raw.get("path", "."),
        compose_file=raw.get("compose_file", "docker-compose.yml"),
        env_file=env_file,
        sync_policy=sync_policy,
        pull_policy=pull_policy,
        health_check_delay_seconds=health_check_delay_seconds,
        notify_url=notify_url,
        enabled=bool(enabled),
        source_file=manifest_path,
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def ensure_repo(
    url: str, local_path: Path, branch: Optional[str] = None, tag: Optional[str] = None
) -> Repo:
    """Clone repo if it doesn't exist, otherwise return existing repo."""
    if local_path.exists():
        try:
            repo = Repo(local_path)
            log.debug("Using existing repo at %s", local_path)
            return repo
        except InvalidGitRepositoryError:
            log.warning("Directory %s exists but is not a git repo, re-cloning", local_path)
            import shutil

            shutil.rmtree(local_path)

    log.info("Cloning %s → %s", strip_url_credentials(url), local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo.clone_from(url, local_path)
    if tag:
        repo.git.checkout(tag)
    elif branch:
        repo.git.checkout(branch)
    return repo


def fetch_ref(repo: Repo, ref: AppRef) -> bool:
    """Fetch refs from origin so SHA comparison can run against fresh remote state."""
    try:
        if ref.tag:
            repo.git.fetch("origin", "--tags")
        else:
            repo.git.fetch("origin")
        return True
    except GitCommandError as e:
        log.error("git fetch failed: %s", strip_url_credentials(str(e)))
        return False


def get_remote_sha(repo: Repo, ref: AppRef) -> Optional[str]:
    """Return the remote SHA for a branch or tag after refs were fetched."""
    try:
        if ref.branch:
            return repo.remotes.origin.refs[ref.branch].commit.hexsha
        elif ref.tag:
            # Resolve tag to commit SHA
            tag_ref = next((t for t in repo.tags if t.name == ref.tag), None)
            if tag_ref is None:
                log.error("Tag %s not found in repo", ref.tag)
                return None
            return tag_ref.commit.hexsha
    except (IndexError, AttributeError) as e:
        log.error("Could not resolve ref %s: %s", ref, e)
        return None


def check_app(repo: Repo, ref: AppRef) -> CheckResult:
    """Compare local and remote SHAs and return sync status."""
    local_sha = repo.head.commit.hexsha
    remote_sha = get_remote_sha(repo, ref)

    if remote_sha is None:
        return CheckResult(status=SyncStatus.UNKNOWN, local_sha=local_sha)

    if local_sha == remote_sha:
        return CheckResult(
            status=SyncStatus.SYNCED,
            local_sha=local_sha,
            remote_sha=remote_sha,
        )

    return CheckResult(
        status=SyncStatus.OUT_OF_SYNC,
        local_sha=local_sha,
        remote_sha=remote_sha,
    )


def apply_ref(repo: Repo, ref: AppRef) -> bool:
    """Apply remote changes for a branch/tag to the local working tree.

    For branch refs the working copy is forced to converge on ``origin/<branch>``:
    a fast-forward when the local branch is simply behind, otherwise a hard reset
    that discards any local-only commits. Steward treats local working copies as
    caches of the desired state declared in git — a divergent local commit is
    never intentional and is always safe to discard. This self-heal keeps a node
    from wedging on a divergent branch (`git pull` returns exit 128 on git ≥ 2.27).
    """
    try:
        if ref.branch:
            repo.git.fetch("origin", ref.branch)
            remote_ref = f"origin/{ref.branch}"
            try:
                # Fast-forward only; succeeds when local is an ancestor of remote
                # (or already up to date). Fails on any divergence.
                repo.git.merge("--ff-only", remote_ref)
            except GitCommandError:
                # Divergent / non-fast-forward: force the working copy back to the
                # remote tip, discarding local-only commits.
                abandoned = repo.head.commit.hexsha
                log.warning(
                    "Local branch '%s' diverged from %s; hard-resetting and "
                    "discarding local commit %s",
                    ref.branch,
                    remote_ref,
                    abandoned[:8],
                )
                repo.git.reset("--hard", remote_ref)
        elif ref.tag:
            repo.git.checkout(ref.tag)
        return True
    except GitCommandError as e:
        log.error("git pull/checkout failed: %s", strip_url_credentials(str(e)))
        return False


def sync_repo(repo: Repo, ref: AppRef) -> Optional[bool]:
    """
    Check if remote is ahead of local. Pull if so.
    Returns True if updated, False if already up to date, None on error.
    """
    if not fetch_ref(repo, ref):
        log.warning("Could not determine remote SHA, skipping sync")
        return None

    check = check_app(repo, ref)

    if check.status == SyncStatus.UNKNOWN:
        log.warning("Could not determine remote SHA, skipping sync")
        return None

    if check.status == SyncStatus.SYNCED:
        local_sha = check.local_sha or ""
        log.debug("Repo at %s is up to date (%s)", repo.working_dir, local_sha[:8])
        return False

    local_sha = check.local_sha or ""
    remote_sha = check.remote_sha or ""
    log.info(
        "Repo %s has changes: %s → %s",
        repo.working_dir,
        local_sha[:8],
        remote_sha[:8],
    )

    if not apply_ref(repo, ref):
        return None
    return True


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------


def _is_self_update(app: AppManifest) -> bool:
    """True when reconciling the steward stack itself — compose up would kill this process."""
    return app.name == AGENT_CONTAINER_NAME


def _get_helper_image() -> Optional[str]:
    """Return the image of the running steward container via docker inspect."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", AGENT_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def spawn_compose_helper(app: AppManifest, stack_path: Path) -> bool:
    """
    Spawn a short-lived peer container that runs docker compose up after a brief delay.

    When steward updates itself, calling docker compose up -d directly sends SIGTERM to
    the running container before the replacement is created. The helper is independent of
    steward's process, so the kill does not abort the compose operation.

    Falls back to run_compose() if host paths cannot be resolved (e.g. in dev/test setups
    where AGENT_CONTAINER_NAME does not match a real container).
    """
    helper_image = _get_helper_image()
    if not helper_image:
        log.warning(
            "Self-update: docker inspect '%s' returned no image — falling back to direct compose",
            AGENT_CONTAINER_NAME,
        )
        return run_compose(app, stack_path)

    # Translate the container-internal compose file path to the host filesystem path.
    # The helper runs as a peer container and can only see host paths.
    container_compose_file = stack_path / app.path / app.compose_file
    host_compose_file = _resolve_host_path(container_compose_file)
    host_root = _resolve_host_path(GITOPS_ROOT)

    if not host_compose_file or not host_root:
        log.warning(
            "Self-update: cannot resolve host paths (AGENT_CONTAINER_NAME='%s') — falling back to direct compose",
            AGENT_CONTAINER_NAME,
        )
        return run_compose(app, stack_path)

    inner_parts = [
        "docker",
        "compose",
        "--project-name",
        shlex.quote(app.name),
        "-f",
        shlex.quote(host_compose_file),
    ]

    # Include override file if present so secrets and host-specific settings survive self-update.
    # Docker Compose only auto-discovers override files when no explicit -f is given.
    container_override_file = stack_path / app.path / "docker-compose.override.yml"
    if container_override_file.exists():
        host_override_file = _resolve_host_path(container_override_file)
        if host_override_file:
            inner_parts += ["-f", shlex.quote(host_override_file)]
            log.debug("Self-update: including override file %s", host_override_file)
        else:
            log.warning(
                "Self-update: override file found but host path could not be resolved — secrets may be missing"
            )

    if app.env_file:
        host_env = _resolve_host_path(Path(app.env_file))
        if host_env:
            inner_parts += ["--env-file", shlex.quote(host_env)]
        else:
            log.warning(
                "Self-update: cannot resolve host path for env_file '%s', omitting from helper",
                app.env_file,
            )
    inner_parts += [
        "up",
        "-d",
        "--remove-orphans",
        "--pull",
        shlex.quote(app.pull_policy),
    ]

    inner_cmd = " ".join(inner_parts)

    helper_run = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--entrypoint",
        "sh",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{host_root}:{host_root}",
        "-e",
        "HOME=/tmp",
        helper_image,
        "-c",
        f"sleep 5 && timeout 300 {inner_cmd}",
    ]

    log.info("Self-update: spawning helper container (image=%s)", helper_image)
    log.debug("Helper command: %s", " ".join(helper_run))

    try:
        result = subprocess.run(helper_run, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("Self-update: failed to start helper: %s", result.stderr.strip())
            return False
        log.info(
            "Self-update: helper launched (%s) — steward will restart momentarily",
            result.stdout.strip()[:12],
        )
        return True
    except Exception as e:
        log.error("Self-update: error launching helper: %s", e)
        return False


def run_compose(app: AppManifest, stack_path: Path) -> bool:
    """
    Run docker compose with an explicit project name and configured pull policy.
    Returns True on success, False on failure.
    """
    compose_file = stack_path / app.path / app.compose_file
    log.debug("App '%s' | inside  compose_file: %s", app.name, compose_file)
    log.debug("App '%s' | outside compose_file: %s", app.name, host_path(compose_file))

    if not compose_file.exists():
        log.error("Compose file not found: %s", compose_file)
        return False

    cmd = [
        "docker",
        "compose",
        "--project-name",
        app.name,
        "-f",
        str(compose_file),
    ]

    env = os.environ.copy()
    if app.env_file:
        env_path = Path(app.env_file)
        log.debug("App '%s' | inside  env_file: %s", app.name, env_path)
        log.debug("App '%s' | outside env_file: %s", app.name, host_path(env_path))
        if not env_path.exists():
            log.error(
                "env_file not found for app '%s' | inside: %s | outside: %s",
                app.name,
                env_path,
                host_path(env_path),
            )
            return False
        cmd.extend(["--env-file", str(env_path)])

    cmd.extend(["up", "-d", "--remove-orphans", "--pull", app.pull_policy])

    log.info("Reconciling app '%s': %s", app.name, " ".join(cmd))

    workdir = stack_path / app.path

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(workdir),
            timeout=300,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                log.info("[compose/%s] %s", app.name, line)
        if result.stderr:
            for line in result.stderr.strip().splitlines():
                log.warning("[compose/%s] %s", app.name, line)
        if result.returncode != 0:
            log.error(
                "docker compose exited with code %d for app '%s'", result.returncode, app.name
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("docker compose timed out for app '%s'", app.name)
        return False
    except FileNotFoundError:
        log.error("docker compose not found — is Docker installed?")
        return False


def _load_compose_services_status(app: AppManifest, stack_path: Path) -> Optional[list[dict]]:
    """Return docker compose ps service JSON rows, or None on failure."""
    compose_file = stack_path / app.path / app.compose_file
    if not compose_file.exists():
        log.error("Compose file not found for health check: %s", compose_file)
        return None

    cmd = [
        "docker",
        "compose",
        "--project-name",
        app.name,
        "-f",
        str(compose_file),
    ]
    if app.env_file:
        env_path = Path(app.env_file)
        if not env_path.exists():
            log.error("env_file not found for health check: %s", env_path)
            return None
        cmd.extend(["--env-file", str(env_path)])
    cmd.extend(["ps", "--format", "json"])

    workdir = stack_path / app.path

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            cwd=str(workdir),
            timeout=60,
        )
        if result.returncode != 0:
            log.warning(
                "docker compose ps failed for app '%s': %s", app.name, result.stderr.strip()
            )
            return None

        raw = result.stdout.strip()
        if not raw:
            return []

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            rows = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    log.warning(
                        "Unparseable docker compose ps line for app '%s': %s", app.name, line
                    )
                    return None
                if isinstance(row, dict):
                    rows.append(row)
            return rows
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("docker compose ps failed for app '%s': %s", app.name, e)
        return None

    return None


def _load_expected_services(app: AppManifest, stack_path: Path) -> Optional[set[str]]:
    """Return expected service names from desired compose config, or None on error."""
    compose_file = stack_path / app.path / app.compose_file
    if not compose_file.exists():
        log.error("Compose file not found for drift check: %s", compose_file)
        return None

    cmd = [
        "docker",
        "compose",
        "--project-name",
        app.name,
        "-f",
        str(compose_file),
    ]
    if app.env_file:
        env_path = Path(app.env_file)
        if not env_path.exists():
            log.error("env_file not found for drift check: %s", env_path)
            return None
        cmd.extend(["--env-file", str(env_path)])
    cmd.extend(["config", "--services"])

    workdir = stack_path / app.path

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            cwd=str(workdir),
            timeout=60,
        )
        if result.returncode != 0:
            log.warning(
                "docker compose config --services failed for app '%s': %s",
                app.name,
                result.stderr.strip(),
            )
            return None

        services = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return services
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("docker compose config --services failed for app '%s': %s", app.name, e)
        return None


def _detect_live_drift(app: AppManifest, stack_path: Path) -> tuple[bool, str]:
    """Detect drift between expected services and live running services."""
    expected = _load_expected_services(app, stack_path)
    if expected is None:
        return False, "expected_services_unavailable"

    service_rows = _load_compose_services_status(app, stack_path)
    if service_rows is None:
        return False, "live_state_unavailable"

    live_states: dict[str, str] = {}
    for row in service_rows:
        name = str(row.get("Service") or row.get("Name") or "").strip()
        if not name:
            continue
        live_states[name] = str(row.get("State", "")).strip().lower()

    missing_or_stopped: list[str] = []
    for service in sorted(expected):
        state = live_states.get(service)
        if state == "running":
            continue
        missing_or_stopped.append(f"{service}:{state or 'missing'}")

    if not missing_or_stopped:
        return False, ""

    return True, f"live_drift_detected[{', '.join(missing_or_stopped)}]"


def _classify_health_status(service_rows: list[dict]) -> str:
    """Classify compose service states into Healthy/Degraded/Unknown."""
    if not service_rows:
        return HEALTH_STATUS_UNKNOWN

    saw_running = False
    for svc in service_rows:
        state = str(svc.get("State", "")).strip().lower()
        exit_code_raw = svc.get("ExitCode")
        try:
            exit_code = int(exit_code_raw) if exit_code_raw is not None else None
        except (TypeError, ValueError):
            exit_code = None

        if "restarting" in state:
            return HEALTH_STATUS_DEGRADED
        if state == "running":
            saw_running = True
            continue
        if state == "exited" and exit_code == 0:
            # Treat successful oneshot containers as neutral for health.
            continue
        if state in {"exited", "dead"}:
            return HEALTH_STATUS_DEGRADED
        if state and state != "running":
            return HEALTH_STATUS_DEGRADED

    if saw_running or service_rows:
        return HEALTH_STATUS_HEALTHY
    return HEALTH_STATUS_UNKNOWN


def _evaluate_health_status(app: AppManifest, stack_path: Path, app_state: dict) -> str:
    """Return current health status from compose ps output."""
    last_sync = app_state.get("last_sync_timestamp")
    if last_sync is None:
        return HEALTH_STATUS_UNKNOWN

    if time.time() - float(last_sync) < app.health_check_delay_seconds:
        return HEALTH_STATUS_PROGRESSING

    service_rows = _load_compose_services_status(app, stack_path)
    if service_rows is None:
        return HEALTH_STATUS_UNKNOWN
    return _classify_health_status(service_rows)


def sync_app(app: AppManifest, repo: Repo, stack_path: Path) -> SyncResult:
    """Apply git changes and run compose for one app."""
    if not apply_ref(repo, app.ref):
        return SyncResult(success=False, message="git_apply_failed")

    log.info("App '%s' repo updated, running compose", app.name)
    if _is_self_update(app):
        success = spawn_compose_helper(app, stack_path)
    else:
        success = run_compose(app, stack_path)

    if not success:
        return SyncResult(success=False, message="compose_failed")

    return SyncResult(success=True, message="synced")


# ---------------------------------------------------------------------------
# Main reconciliation loop
# ---------------------------------------------------------------------------


def load_node_manifests(
    control_repo: Repo,
) -> tuple[list[AppManifest], list[tuple[str, str, str]]]:
    """
    Load all app manifests for this node from the control repo.
    Returns (manifests, parse_error_entries) where each error entry is a
    (filename, app_name, error_msg) tuple. app_name is extracted from the raw
    YAML ``name`` key, falling back to the file stem when parsing fails.
    """
    node_dir = Path(control_repo.working_dir) / "nodes" / GITOPS_NODE_NAME
    if not node_dir.exists():
        log.warning("No manifest directory found for node '%s' at %s", GITOPS_NODE_NAME, node_dir)
        return [], []

    manifests: list[AppManifest] = []
    parse_error_entries: list[tuple[str, str, str]] = []
    for manifest_file in sorted(node_dir.glob("*.yml")):
        try:
            manifest = parse_manifest(manifest_file)
            manifests.append(manifest)
            log.debug("Loaded manifest: %s (enabled=%s)", manifest.name, manifest.enabled)
        except (ValueError, yaml.YAMLError) as e:
            # Best-effort app name extraction so we can track the error in metrics.
            try:
                raw = yaml.safe_load(manifest_file.read_text())
                app_name = raw.get("name") if isinstance(raw, dict) else None
            except Exception:
                app_name = None
            app_name = app_name or manifest_file.stem
            log.error("Skipping invalid manifest %s: %s", manifest_file.name, e)
            parse_error_entries.append((manifest_file.name, app_name, str(e)))

    log.info(
        "Loaded %d manifest(s) for node '%s' (%d parse error(s))",
        len(manifests),
        GITOPS_NODE_NAME,
        len(parse_error_entries),
    )
    return manifests, parse_error_entries


def reconcile_app(app: AppManifest, state: dict) -> bool:
    """
    Reconcile a single app. Returns True on success.
    Clones stack repo if needed, syncs, runs compose if changed.
    Updates metrics state in-place.
    """
    stack_path = STACKS_DIR / app.name
    log.debug("App '%s' | inside  stack_path: %s", app.name, stack_path)
    log.debug("App '%s' | outside stack_path: %s", app.name, host_path(stack_path))

    app_state = state.setdefault("apps", {}).setdefault(app.name, {})
    app_state.update(
        {
            "repo": strip_url_credentials(app.repo),
            "ref": app.ref.branch or app.ref.tag or "",
            "ref_type": "branch" if app.ref.branch else "tag",
            "sync_policy": app.sync_policy,
            "health_check_delay_seconds": app.health_check_delay_seconds,
            "enabled": app.enabled,
        }
    )
    app_state["last_reconcile_timestamp"] = time.time()

    # Ensure stack repo is cloned
    try:
        repo = ensure_repo(
            url=app.repo,
            local_path=stack_path,
            branch=app.ref.branch,
            tag=app.ref.tag,
        )
    except GitCommandError as e:
        log.error("Failed to clone repo for app '%s': %s", app.name, strip_url_credentials(str(e)))
        app_state["sync_status"] = SyncStatus.UNKNOWN.value
        app_state["health_status"] = HEALTH_STATUS_UNKNOWN
        _inc(app_state, "reconcile_total", "failed")
        return False

    # Use try/finally to ensure the Repo object is always closed. GitPython keeps
    # persistent 'git cat-file --batch' processes alive for the lifetime of the Repo
    # object. Without explicit close(), these accumulate as zombies across cron runs.
    try:
        if not fetch_ref(repo, app.ref):
            app_state["sync_status"] = SyncStatus.UNKNOWN.value
            app_state["health_status"] = HEALTH_STATUS_UNKNOWN
            _inc(app_state, "reconcile_total", "failed")
            return False

        check = check_app(repo, app.ref)
        app_state["deployed_sha"] = check.local_sha
        app_state["remote_sha"] = check.remote_sha

        if check.status == SyncStatus.UNKNOWN:
            app_state["sync_status"] = SyncStatus.UNKNOWN.value
            app_state["health_status"] = HEALTH_STATUS_UNKNOWN
            _inc(app_state, "reconcile_total", "failed")
            return False

        if check.status == SyncStatus.SYNCED:
            drifted, drift_reason = _detect_live_drift(app, stack_path)
            if drift_reason in ("expected_services_unavailable", "live_state_unavailable"):
                log.warning(
                    "App '%s' live state cannot be determined (%s), reporting Unknown",
                    app.name,
                    drift_reason,
                )
                app_state["sync_status"] = SyncStatus.UNKNOWN.value
                app_state["health_status"] = HEALTH_STATUS_UNKNOWN
                _inc(app_state, "reconcile_total", "failed")
                return False
            if drifted:
                app_state["sync_status"] = SyncStatus.OUT_OF_SYNC.value

                if app.sync_policy == "manual":
                    log.info(
                        "App '%s' has live drift but sync_policy=manual, skipping self-heal",
                        app.name,
                    )
                    app_state["health_status"] = _evaluate_health_status(app, stack_path, app_state)
                    _inc(app_state, "reconcile_total", "skipped")
                    _append_operation(
                        state,
                        app=app,
                        trigger="self_heal",
                        from_sha=check.local_sha,
                        to_sha=check.remote_sha,
                        sync_status="Skipped",
                        message=f"manual_policy_skip:{drift_reason}",
                        health_status=app_state.get("health_status"),
                    )
                    _send_notification(
                        app,
                        "drift_detected",
                        {
                            "sync_policy": app.sync_policy,
                            "sync_status": app_state["sync_status"],
                            "health_status": app_state.get("health_status"),
                            "deployed_sha": check.local_sha,
                            "remote_sha": check.remote_sha,
                            "message": drift_reason,
                        },
                    )
                    return True

                log.warning(
                    "App '%s' has live drift, attempting self-heal: %s", app.name, drift_reason
                )
                if STEWARD_DRY_RUN:
                    app_state["health_status"] = _evaluate_health_status(app, stack_path, app_state)
                    _inc(app_state, "reconcile_total", "skipped")
                    _append_operation(
                        state,
                        app=app,
                        trigger="self_heal",
                        from_sha=check.local_sha,
                        to_sha=check.remote_sha,
                        sync_status="Skipped",
                        message=f"dry_run_skip:{drift_reason}",
                        health_status=app_state.get("health_status"),
                    )
                    _send_notification(
                        app,
                        "drift_detected",
                        {
                            "sync_policy": app.sync_policy,
                            "sync_status": app_state["sync_status"],
                            "health_status": app_state.get("health_status"),
                            "deployed_sha": check.local_sha,
                            "remote_sha": check.remote_sha,
                            "message": f"dry-run mode: {drift_reason}",
                        },
                    )
                    return True

                healed = (
                    spawn_compose_helper(app, stack_path)
                    if _is_self_update(app)
                    else run_compose(app, stack_path)
                )
                _inc(app_state, "sync_total", "success" if healed else "failed")
                _append_operation(
                    state,
                    app=app,
                    trigger="self_heal",
                    from_sha=check.local_sha,
                    to_sha=check.remote_sha,
                    sync_status="Synced" if healed else "Failed",
                    message=drift_reason if healed else f"self_heal_failed:{drift_reason}",
                    health_status=None,
                )

                if not healed:
                    app_state["health_status"] = HEALTH_STATUS_DEGRADED
                    _inc(app_state, "reconcile_total", "failed")
                    _send_notification(
                        app,
                        "sync_failed",
                        {
                            "sync_policy": app.sync_policy,
                            "sync_status": app_state["sync_status"],
                            "health_status": app_state.get("health_status"),
                            "deployed_sha": check.local_sha,
                            "remote_sha": check.remote_sha,
                            "message": drift_reason,
                        },
                    )
                    return False

                log.warning("out-of-band drift detected and healed for app %s", app.name)
                _inc(app_state, "ooband_heal_total")
                app_state["sync_status"] = SyncStatus.SYNCED.value
                app_state["last_sync_timestamp"] = time.time()
                app_state["health_status"] = HEALTH_STATUS_PROGRESSING
                _inc(app_state, "reconcile_total", "success")
                return True

            log.info("App '%s' is up to date, no action needed", app.name)
            app_state["sync_status"] = SyncStatus.SYNCED.value
            app_state["health_status"] = _evaluate_health_status(app, stack_path, app_state)
            if app_state["health_status"] == HEALTH_STATUS_DEGRADED:
                _send_notification(
                    app,
                    "health_degraded",
                    {
                        "sync_policy": app.sync_policy,
                        "sync_status": app_state["sync_status"],
                        "health_status": app_state["health_status"],
                        "deployed_sha": check.local_sha,
                        "remote_sha": check.remote_sha,
                        "message": "service health degraded",
                    },
                )
            _inc(app_state, "reconcile_total", "success")
            return True

        local_sha = check.local_sha or ""
        remote_sha = check.remote_sha or ""
        log.info(
            "Repo %s has changes: %s → %s",
            repo.working_dir,
            local_sha[:8],
            remote_sha[:8],
        )

        if app.sync_policy == "manual":
            log.info(
                "App '%s' is out of sync, but sync_policy=manual so apply is skipped", app.name
            )
            app_state["sync_status"] = SyncStatus.OUT_OF_SYNC.value
            app_state["health_status"] = _evaluate_health_status(app, stack_path, app_state)
            _inc(app_state, "reconcile_total", "skipped")
            _append_operation(
                state,
                app=app,
                trigger="git_change",
                from_sha=local_sha,
                to_sha=remote_sha,
                sync_status="Skipped",
                message="manual_policy_skip",
                health_status=app_state.get("health_status"),
            )
            _send_notification(
                app,
                "drift_detected",
                {
                    "sync_policy": app.sync_policy,
                    "sync_status": app_state["sync_status"],
                    "health_status": app_state.get("health_status"),
                    "deployed_sha": local_sha,
                    "remote_sha": remote_sha,
                    "message": "git drift detected while sync_policy=manual",
                },
            )
            return True

        if STEWARD_DRY_RUN:
            log.info(
                "App '%s' is out of sync, but STEWARD_DRY_RUN=true so apply is skipped", app.name
            )
            app_state["sync_status"] = SyncStatus.OUT_OF_SYNC.value
            app_state["health_status"] = _evaluate_health_status(app, stack_path, app_state)
            _inc(app_state, "reconcile_total", "skipped")
            _append_operation(
                state,
                app=app,
                trigger="git_change",
                from_sha=local_sha,
                to_sha=remote_sha,
                sync_status="Skipped",
                message="dry_run_skip",
                health_status=app_state.get("health_status"),
            )
            _send_notification(
                app,
                "drift_detected",
                {
                    "sync_policy": app.sync_policy,
                    "sync_status": app_state["sync_status"],
                    "health_status": app_state.get("health_status"),
                    "deployed_sha": local_sha,
                    "remote_sha": remote_sha,
                    "message": "git drift detected while STEWARD_DRY_RUN=true",
                },
            )
            return True

        sync = sync_app(app, repo, stack_path)
        _inc(app_state, "sync_total", "success" if sync.success else "failed")
        _append_operation(
            state,
            app=app,
            trigger="git_change",
            from_sha=local_sha,
            to_sha=remote_sha,
            sync_status="Synced" if sync.success else "Failed",
            message=sync.message,
            health_status=None,
        )
        if sync.success:
            app_state["sync_status"] = SyncStatus.SYNCED.value
            app_state["deployed_sha"] = remote_sha
            app_state["last_sync_timestamp"] = time.time()
            app_state["health_status"] = HEALTH_STATUS_PROGRESSING
            _inc(app_state, "reconcile_total", "success")
        else:
            app_state["sync_status"] = SyncStatus.OUT_OF_SYNC.value
            app_state["health_status"] = HEALTH_STATUS_UNKNOWN
            _inc(app_state, "reconcile_total", "failed")
            _send_notification(
                app,
                "sync_failed",
                {
                    "sync_policy": app.sync_policy,
                    "sync_status": app_state["sync_status"],
                    "health_status": app_state.get("health_status"),
                    "deployed_sha": local_sha,
                    "remote_sha": remote_sha,
                    "message": sync.message,
                },
            )
        return sync.success
    finally:
        repo.close()


def reconcile() -> int:
    """
    Main entry point for a reconciliation run.
    Returns exit code (0 = success, 1 = partial failure, 2 = fatal).
    """
    if not CONTROL_REPO_URL:
        log.error("CONTROL_REPO_URL is not set")
        return 2

    url_err = validate_repo_url(CONTROL_REPO_URL, context="CONTROL_REPO_URL")
    if url_err:
        log.error(url_err)
        return 2

    start_time = time.time()
    state = _load_metrics_state()
    state["node"] = GITOPS_NODE_NAME

    GITOPS_ROOT.mkdir(parents=True, exist_ok=True)
    STACKS_DIR.mkdir(parents=True, exist_ok=True)
    _maybe_warn_legacy_json_state()

    log.info(
        "Starting reconciliation | node=%s root=%s",
        GITOPS_NODE_NAME,
        GITOPS_ROOT,
    )
    log.debug("inside  GITOPS_ROOT    : %s", GITOPS_ROOT)
    log.debug("outside GITOPS_ROOT    : %s", host_path(GITOPS_ROOT))
    log.debug("inside  CONTROL_REPO   : %s", CONTROL_REPO_DIR)
    log.debug("outside CONTROL_REPO   : %s", host_path(CONTROL_REPO_DIR))
    log.debug("inside  STACKS_DIR     : %s", STACKS_DIR)
    log.debug("outside STACKS_DIR     : %s", host_path(STACKS_DIR))
    log_mounts()

    # Step 1: sync control repo
    try:
        control_repo = ensure_repo(
            url=CONTROL_REPO_URL,
            local_path=CONTROL_REPO_DIR,
            branch=CONTROL_REPO_BRANCH,
        )
    except GitCommandError as e:
        log.error("Failed to clone/open control repo: %s", strip_url_credentials(str(e)))
        _inc(state, "reconcile", "total", "fatal")
        state.setdefault("reconcile", {})["last_timestamp"] = time.time()
        _save_metrics_state(state)
        return 2

    control_ref = AppRef(branch=CONTROL_REPO_BRANCH)
    ctrl_sync = sync_repo(control_repo, control_ref)
    ctrl_result = "failed" if ctrl_sync is None else ("updated" if ctrl_sync else "up_to_date")
    _inc(state, "reconcile", "control_repo_sync_total", ctrl_result)
    if ctrl_sync is None:
        log.warning("Control repo sync failed; continuing with cached manifests")

    # Step 2: load manifests for this node
    manifests, parse_error_entries = load_node_manifests(control_repo)
    if parse_error_entries:
        _inc(state, "reconcile", "manifest_parse_errors", by=len(parse_error_entries))

    if not manifests and not parse_error_entries:
        log.info("No apps to reconcile")
        end_time = time.time()
        rec = state.setdefault("reconcile", {})
        rec["last_timestamp"] = end_time
        rec["last_duration_seconds"] = end_time - start_time
        _inc(state, "reconcile", "total", "success")
        _save_metrics_state(state)
        return 0

    # Step 3: reconcile each enabled app
    results: dict[str, str] = {}

    # Treat parse-error apps as failed so they appear in metrics and alerts.
    for _filename, app_name, _err_msg in parse_error_entries:
        app_state = state.setdefault("apps", {}).setdefault(app_name, {})
        app_state.update(
            {
                "enabled": True,
                "sync_status": SyncStatus.UNKNOWN.value,
                "health_status": HEALTH_STATUS_UNKNOWN,
                "last_reconcile_timestamp": time.time(),
            }
        )
        _inc(app_state, "reconcile_total", "failed")
        results[app_name] = "failed"

    for app in manifests:
        if not app.enabled:
            log.info("App '%s' is disabled, skipping", app.name)
            app_state = state.setdefault("apps", {}).setdefault(app.name, {})
            app_state.update(
                {
                    "repo": strip_url_credentials(app.repo),
                    "ref": app.ref.branch or app.ref.tag or "",
                    "ref_type": "branch" if app.ref.branch else "tag",
                    "sync_policy": app.sync_policy,
                    "enabled": False,
                    "sync_status": SYNC_STATUS_DISABLED,
                    "health_status": HEALTH_STATUS_UNKNOWN,
                }
            )
            _inc(app_state, "reconcile_total", "skipped")
            results[app.name] = "disabled"
            continue
        try:
            success = reconcile_app(app, state)
        except Exception as e:
            log.error("Unexpected error reconciling app '%s': %s", app.name, e, exc_info=True)
            app_state = state.setdefault("apps", {}).setdefault(app.name, {})
            app_state["sync_status"] = SyncStatus.UNKNOWN.value
            app_state["health_status"] = HEALTH_STATUS_UNKNOWN
            _inc(app_state, "reconcile_total", "failed")
            success = False
        results[app.name] = "ok" if success else "failed"

    # Observed state is exposed via Prometheus + SQLite only; steward never writes
    # back to the control repo (see plan.md "drop git status writeback").
    try:
        control_repo.close()
    except Exception:
        pass

    # Summary
    failed = [name for name, status in results.items() if status == "failed"]
    log.info(
        "Reconciliation complete | total=%d disabled=%d ok=%d failed=%d parse_errors=%d",
        len(results),
        sum(1 for s in results.values() if s == "disabled"),
        sum(1 for s in results.values() if s == "ok"),
        len(failed),
        len(parse_error_entries),
    )

    end_time = time.time()
    rec = state.setdefault("reconcile", {})
    rec["last_timestamp"] = end_time
    rec["last_duration_seconds"] = end_time - start_time
    run_result = "partial_failure" if failed else "success"
    _inc(state, "reconcile", "total", run_result)
    _save_metrics_state(state)

    if failed:
        log.warning("Failed apps: %s", ", ".join(failed))
        return 1

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(reconcile())
