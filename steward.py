#!/usr/bin/env python3
"""
steward
Watches a control repo for app manifests and reconciles docker compose stacks.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import yaml
from git import Repo, GitCommandError, InvalidGitRepositoryError

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
# Path helpers (inside ↔ outside)
# ---------------------------------------------------------------------------

def _container_mounts() -> list[dict]:
    """Return the Mounts list from docker inspect on this container, or []."""
    container_name = os.environ.get("AGENT_CONTAINER_NAME")
    if not container_name:
        return []
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) or []
    except Exception:
        pass
    return []


def host_path(container_path: Path) -> str:
    """
    Resolve a container path to its host-side source by inspecting mounts.
    Returns a human-readable string; never raises.
    """
    mounts = _container_mounts()
    if not mounts:
        return "<host path unknown: set AGENT_CONTAINER_NAME>"

    best: dict = {}
    best_len = 0
    for mount in mounts:
        dest = mount.get("Destination", "")
        if str(container_path).startswith(dest) and len(dest) > best_len:
            best = mount
            best_len = len(dest)

    if not best:
        return "<not mounted>"

    rel = str(container_path)[best_len:].lstrip("/")
    source = best.get("Source", "")
    name = best.get("Name", "")
    outside = source + ("/" + rel if rel else "")
    if name:
        return f"{outside}  [volume: {name}]"
    return outside


def log_mounts() -> None:
    """Log all container mounts at DEBUG level."""
    mounts = _container_mounts()
    if not mounts:
        log.debug("Mount map unavailable (AGENT_CONTAINER_NAME not set or docker inspect failed)")
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
    try:
        return json.loads(METRICS_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_metrics_state(state: dict) -> None:
    try:
        METRICS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = METRICS_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.rename(METRICS_STATE_FILE)
    except Exception as e:
        log.warning("Failed to save metrics state: %s", e)


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
METRICS_STATE_FILE  = GITOPS_ROOT / "metrics" / "state.json"

# ---------------------------------------------------------------------------
# App manifest schema
# ---------------------------------------------------------------------------

SUPPORTED_VERSIONS = {1}


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

    if errors:
        raise ValueError(f"Invalid manifest {manifest_path}: {'; '.join(errors)}")

    return AppManifest(
        version=version,
        name=name,
        repo=repo,
        ref=ref,
        path=raw.get("path", "."),
        compose_file=raw.get("compose_file", "docker-compose.yml"),
        env_file=raw.get("env_file") or None,
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


def get_remote_sha(repo: Repo, ref: AppRef) -> Optional[str]:
    """Fetch and return the remote SHA for a branch or tag."""
    try:
        repo.remotes.origin.fetch(tags=bool(ref.tag))
    except GitCommandError as e:
        log.error("git fetch failed: %s", e)
        return None

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


def sync_repo(repo: Repo, ref: AppRef) -> Optional[bool]:
    """
    Check if remote is ahead of local. Pull if so.
    Returns True if updated, False if already up to date, None on error.
    """
    local_sha = repo.head.commit.hexsha
    remote_sha = get_remote_sha(repo, ref)

    if remote_sha is None:
        log.warning("Could not determine remote SHA, skipping sync")
        return None

    if local_sha == remote_sha:
        log.debug("Repo at %s is up to date (%s)", repo.working_dir, local_sha[:8])
        return False

    log.info(
        "Repo %s has changes: %s → %s",
        repo.working_dir,
        local_sha[:8],
        remote_sha[:8],
    )

    try:
        if ref.branch:
            repo.remotes.origin.pull(ref.branch)
        elif ref.tag:
            repo.git.checkout(ref.tag)
        return True
    except GitCommandError as e:
        log.error("git pull/checkout failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------

def run_compose(app: AppManifest, stack_path: Path) -> bool:
    """
    Run docker compose up -d --remove-orphans --pull always.
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
        "-f", str(compose_file),
        "up", "-d",
        "--remove-orphans",
        "--pull", "always",
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
        _inc(app_state, "reconcile_total", "failed")
        return False

    # Check for changes and sync
    updated = sync_repo(repo, app.ref)

    if updated is None:
        _inc(app_state, "reconcile_total", "failed")
        return False
    elif updated:
        log.info("App '%s' repo updated, running compose", app.name)
        success = run_compose(app, stack_path)
        _inc(app_state, "sync_total", "success" if success else "failed")
        if success:
            app_state["last_sync_timestamp"] = time.time()
            _inc(app_state, "reconcile_total", "success")
        else:
            _inc(app_state, "reconcile_total", "failed")
        return success
    else:
        log.info("App '%s' is up to date, no action needed", app.name)
        _inc(app_state, "reconcile_total", "success")
        return True


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
                "enabled": False,
            })
            _inc(app_state, "reconcile_total", "skipped")
            results[app.name] = "disabled"
            continue
        try:
            success = reconcile_app(app, state)
        except Exception as e:
            log.error("Unexpected error reconciling app '%s': %s", app.name, e, exc_info=True)
            app_state = state.setdefault("apps", {}).setdefault(app.name, {})
            _inc(app_state, "reconcile_total", "failed")
            success = False
        results[app.name] = "ok" if success else "failed"

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
