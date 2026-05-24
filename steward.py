#!/usr/bin/env python3
"""
steward
Watches a control repo for app manifests and reconciles docker compose stacks.
"""

import json
import logging
import os
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
            capture_output=True, text=True, timeout=5,
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
    """Remove embedded credentials from a repo URL, safe for use as a metric label."""
    try:
        p = urlparse(url)
        if p.scheme in ("http", "https") and p.username:
            netloc = p.hostname + (f":{p.port}" if p.port else "")
            return urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass
    return url


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

GITOPS_ROOT         = Path(os.environ.get("GITOPS_ROOT", Path.home() / "git"))
GITOPS_NODE_NAME    = os.environ.get("GITOPS_NODE_NAME", socket.gethostname())
CONTROL_REPO_URL    = os.environ.get("CONTROL_REPO_URL", "")
CONTROL_REPO_BRANCH = os.environ.get("CONTROL_REPO_BRANCH", "main")
CONTROL_REPO_DIR    = GITOPS_ROOT / "control"
STACKS_DIR          = GITOPS_ROOT / "stacks"
DB_FILE             = GITOPS_ROOT / "steward.db"
STEWARD_NOTIFY_URL  = os.environ.get("STEWARD_NOTIFY_URL", "")
STEWARD_DRY_RUN     = os.environ.get("STEWARD_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}
LEGACY_METRICS_STATE_FILE = GITOPS_ROOT / "metrics" / "state.json"
LEGACY_NOTICE_MARKER_FILE = GITOPS_ROOT / "metrics" / ".legacy_json_ignored"

# ---------------------------------------------------------------------------
# App manifest schema
# ---------------------------------------------------------------------------

SUPPORTED_VERSIONS = {1, 2}
SUPPORTED_SYNC_POLICIES = {"auto", "manual"}
SUPPORTED_PULL_POLICIES = {"always", "missing", "never"}


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
                log.warning("Notification endpoint returned HTTP %s for app '%s'", resp.status, app.name)
    except (error.URLError, TimeoutError, ValueError) as e:
        log.warning("Notification send failed for app '%s': %s", app.name, e)


def _ts_to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return None


def _build_status_snapshot(state: dict) -> dict:
    apps_payload: dict[str, dict] = {}
    for app_name, app_state in sorted(state.get("apps", {}).items()):
        apps_payload[app_name] = {
            "sync_status": app_state.get("sync_status", SyncStatus.UNKNOWN.value),
            "health_status": app_state.get("health_status", HEALTH_STATUS_UNKNOWN),
            "deployed_sha": app_state.get("deployed_sha"),
            "remote_sha": app_state.get("remote_sha"),
            "last_synced_at": _ts_to_iso(app_state.get("last_sync_timestamp")),
        }

    return {
        "node": GITOPS_NODE_NAME,
        "updated_at": _now_iso(),
        "apps": apps_payload,
    }


def _write_status_snapshot(control_repo: Repo, state: dict) -> bool:
    """Write nodes/<hostname>/status.json and push only when content changed."""
    status_rel = Path("nodes") / GITOPS_NODE_NAME / "status.json"
    status_abs = Path(control_repo.working_dir) / status_rel
    status_abs.parent.mkdir(parents=True, exist_ok=True)

    snapshot = _build_status_snapshot(state)
    rendered = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    current = status_abs.read_text() if status_abs.exists() else None

    if current == rendered:
        return True

    status_abs.write_text(rendered)

    try:
        control_repo.index.add([str(status_rel)])
        control_repo.index.commit(f"steward: update status for {GITOPS_NODE_NAME}")
        control_repo.remotes.origin.push(CONTROL_REPO_BRANCH)
        return True
    except GitCommandError as e:
        log.error("Status writeback failed: %s", e)
        return False


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

def ensure_repo(url: str, local_path: Path, branch: Optional[str] = None, tag: Optional[str] = None) -> Repo:
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

    log.info("Cloning %s → %s", url, local_path)
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
        repo.remotes.origin.fetch(tags=bool(ref.tag))
        return True
    except GitCommandError as e:
        log.error("git fetch failed: %s", e)
        return False


def get_remote_sha(repo: Repo, ref: AppRef) -> Optional[str]:
    """Return the remote SHA for a branch or tag after refs were fetched."""
    try:
        if ref.branch:
            return repo.remotes.origin.refs[ref.branch].commit.hexsha
        elif ref.tag:
            # Resolve tag to commit SHA
            tag_ref = next(
                (t for t in repo.tags if t.name == ref.tag), None
            )
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
    """Apply remote changes for a branch/tag to local working tree."""
    try:
        if ref.branch:
            repo.remotes.origin.pull(ref.branch)
        elif ref.tag:
            repo.git.checkout(ref.tag)
        return True
    except GitCommandError as e:
        log.error("git pull/checkout failed: %s", e)
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
            capture_output=True, text=True, timeout=5,
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
        "docker", "compose",
        "--project-name", shlex.quote(app.name),
        "-f", shlex.quote(host_compose_file),
        "up", "-d",
        "--remove-orphans",
        "--pull", shlex.quote(app.pull_policy),
    ]
    if app.env_file:
        host_env = _resolve_host_path(Path(app.env_file))
        if host_env:
            inner_parts += ["--env-file", shlex.quote(host_env)]
        else:
            log.warning(
                "Self-update: cannot resolve host path for env_file '%s', omitting from helper",
                app.env_file,
            )

    inner_cmd = " ".join(inner_parts)

    helper_run = [
        "docker", "run",
        "--rm", "-d",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{host_root}:{host_root}",
        "-e", "HOME=/tmp",
        helper_image,
        "sh", "-c", f"sleep 5 && {inner_cmd}",
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
        "docker", "compose",
        "--project-name", app.name,
        "-f", str(compose_file),
        "up", "-d",
        "--remove-orphans",
        "--pull", app.pull_policy,
    ]

    env = os.environ.copy()
    if app.env_file:
        env_path = Path(app.env_file)
        log.debug("App '%s' | inside  env_file: %s", app.name, env_path)
        log.debug("App '%s' | outside env_file: %s", app.name, host_path(env_path))
        if not env_path.exists():
            log.error(
                "env_file not found for app '%s' | inside: %s | outside: %s",
                app.name, env_path, host_path(env_path),
            )
            return False
        cmd.extend(["--env-file", str(env_path)])

    log.info("Reconciling app '%s': %s", app.name, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                log.info("[compose/%s] %s", app.name, line)
        if result.stderr:
            for line in result.stderr.strip().splitlines():
                log.warning("[compose/%s] %s", app.name, line)
        if result.returncode != 0:
            log.error("docker compose exited with code %d for app '%s'", result.returncode, app.name)
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
        "docker", "compose",
        "--project-name", app.name,
        "-f", str(compose_file),
        "ps", "--format", "json",
    ]
    if app.env_file:
        env_path = Path(app.env_file)
        if not env_path.exists():
            log.error("env_file not found for health check: %s", env_path)
            return None
        cmd.extend(["--env-file", str(env_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("docker compose ps failed for app '%s': %s", app.name, result.stderr.strip())
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
                    log.warning("Unparseable docker compose ps line for app '%s': %s", app.name, line)
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
        "docker", "compose",
        "--project-name", app.name,
        "-f", str(compose_file),
        "config", "--services",
    ]
    if app.env_file:
        env_path = Path(app.env_file)
        if not env_path.exists():
            log.error("env_file not found for drift check: %s", env_path)
            return None
        cmd.extend(["--env-file", str(env_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("docker compose config --services failed for app '%s': %s", app.name, result.stderr.strip())
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

def load_node_manifests(control_repo: Repo) -> tuple[list[AppManifest], int]:
    """
    Load all app manifests for this node from the control repo.
    Returns (manifests, parse_error_count).
    """
    node_dir = Path(control_repo.working_dir) / "nodes" / GITOPS_NODE_NAME
    if not node_dir.exists():
        log.warning("No manifest directory found for node '%s' at %s", GITOPS_NODE_NAME, node_dir)
        return [], 0

    manifests = []
    parse_errors = 0
    for manifest_file in sorted(node_dir.glob("*.yml")):
        try:
            manifest = parse_manifest(manifest_file)
            manifests.append(manifest)
            log.debug("Loaded manifest: %s (enabled=%s)", manifest.name, manifest.enabled)
        except (ValueError, yaml.YAMLError) as e:
            log.error("Skipping invalid manifest %s: %s", manifest_file.name, e)
            parse_errors += 1

    log.info("Loaded %d manifest(s) for node '%s' (%d parse error(s))", len(manifests), GITOPS_NODE_NAME, parse_errors)
    return manifests, parse_errors


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
    app_state.update({
        "repo": strip_url_credentials(app.repo),
        "ref": app.ref.branch or app.ref.tag or "",
        "ref_type": "branch" if app.ref.branch else "tag",
        "sync_policy": app.sync_policy,
        "health_check_delay_seconds": app.health_check_delay_seconds,
        "enabled": app.enabled,
    })
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
        log.error("Failed to clone repo for app '%s': %s", app.name, e)
        app_state["sync_status"] = SyncStatus.UNKNOWN.value
        app_state["health_status"] = HEALTH_STATUS_UNKNOWN
        _inc(app_state, "reconcile_total", "failed")
        return False

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
        if drifted:
            app_state["sync_status"] = SyncStatus.OUT_OF_SYNC.value

            if app.sync_policy == "manual":
                log.info("App '%s' has live drift but sync_policy=manual, skipping self-heal", app.name)
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

            log.warning("App '%s' has live drift, attempting self-heal: %s", app.name, drift_reason)
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

            healed = run_compose(app, stack_path)
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
        log.info("App '%s' is out of sync, but sync_policy=manual so apply is skipped", app.name)
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
        log.info("App '%s' is out of sync, but STEWARD_DRY_RUN=true so apply is skipped", app.name)
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


def reconcile() -> int:
    """
    Main entry point for a reconciliation run.
    Returns exit code (0 = success, 1 = partial failure, 2 = fatal).
    """
    if not CONTROL_REPO_URL:
        log.error("CONTROL_REPO_URL is not set")
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
        log.error("Failed to clone/open control repo: %s", e)
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
    manifests, parse_errors = load_node_manifests(control_repo)
    if parse_errors:
        _inc(state, "reconcile", "manifest_parse_errors", by=parse_errors)

    if not manifests:
        log.info("No apps to reconcile")
        end_time = time.time()
        rec = state.setdefault("reconcile", {})
        rec["last_timestamp"] = end_time
        rec["last_duration_seconds"] = end_time - start_time
        _inc(state, "reconcile", "total", "success")
        _save_metrics_state(state)
        return 0

    # Step 3: reconcile each enabled app
    results = {}
    for app in manifests:
        if not app.enabled:
            log.info("App '%s' is disabled, skipping", app.name)
            app_state = state.setdefault("apps", {}).setdefault(app.name, {})
            app_state.update({
                "repo": strip_url_credentials(app.repo),
                "ref": app.ref.branch or app.ref.tag or "",
                "ref_type": "branch" if app.ref.branch else "tag",
                "sync_policy": app.sync_policy,
                "enabled": False,
                "sync_status": SYNC_STATUS_DISABLED,
                "health_status": HEALTH_STATUS_UNKNOWN,
            })
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

    # Step 4: write observed status snapshot back to control repo
    status_write_ok = _write_status_snapshot(control_repo, state)
    if not status_write_ok:
        log.warning("Status writeback failed; marking run as partial failure")
        results["_status_writeback"] = "failed"

    # Summary
    failed = [name for name, status in results.items() if status == "failed"]
    log.info(
        "Reconciliation complete | total=%d disabled=%d ok=%d failed=%d",
        len(results),
        sum(1 for s in results.values() if s == "disabled"),
        sum(1 for s in results.values() if s == "ok"),
        len(failed),
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
