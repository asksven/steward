#!/usr/bin/env python3
"""
steward
Watches a control repo for app manifests and reconciles docker compose stacks.
"""

import logging
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from git import Repo, GitCommandError, InvalidGitRepositoryError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("steward")

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

GITOPS_ROOT         = Path(os.environ.get("GITOPS_ROOT", Path.home() / "git"))
GITOPS_NODE_NAME    = os.environ.get("GITOPS_NODE_NAME", socket.gethostname())
CONTROL_REPO_URL    = os.environ.get("CONTROL_REPO_URL", "")
CONTROL_REPO_BRANCH = os.environ.get("CONTROL_REPO_BRANCH", "main")
CONTROL_REPO_DIR    = GITOPS_ROOT / "control"
STACKS_DIR          = GITOPS_ROOT / "stacks"

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


def sync_repo(repo: Repo, ref: AppRef) -> bool:
    """
    Check if remote is ahead of local. Pull if so.
    Returns True if repo was updated, False if already up to date.
    """
    local_sha = repo.head.commit.hexsha
    remote_sha = get_remote_sha(repo, ref)

    if remote_sha is None:
        log.warning("Could not determine remote SHA, skipping sync")
        return False

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
        return False


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------

def run_compose(app: AppManifest, stack_path: Path) -> bool:
    """
    Run docker compose up -d --remove-orphans --pull always.
    Returns True on success, False on failure.
    """
    compose_file = stack_path / app.path / app.compose_file

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
        if not env_path.exists():
            log.error("env_file %s not found for app %s", app.env_file, app.name)
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
# Self-update
# ---------------------------------------------------------------------------

def self_update() -> None:
    """
    Check if a newer image is available for the agent itself and restart
    the agent container if so. Requires AGENT_CONTAINER_NAME env var.
    """
    container_name = os.environ.get("AGENT_CONTAINER_NAME")
    if not container_name:
        log.debug("AGENT_CONTAINER_NAME not set, skipping self-update")
        return

    agent_image = os.environ.get("AGENT_IMAGE")
    if not agent_image:
        log.debug("AGENT_IMAGE not set, skipping self-update")
        return

    log.info("Checking for agent self-update (image: %s)", agent_image)

    try:
        # Pull the latest image
        result = subprocess.run(
            ["docker", "pull", agent_image],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.error("docker pull failed: %s", result.stderr)
            return

        # Get the digest of the running container's image
        running = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
        # Get the digest of the newly pulled image
        latest = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", agent_image],
            capture_output=True, text=True, timeout=10,
        )

        if running.returncode != 0 or latest.returncode != 0:
            log.warning("Could not compare image digests, skipping restart")
            return

        running_digest = running.stdout.strip()
        latest_digest = latest.stdout.strip()

        if running_digest == latest_digest:
            log.info("Agent image is up to date")
            return

        log.info(
            "New agent image detected (%s → %s), restarting container",
            running_digest[:12],
            latest_digest[:12],
        )

        restart = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True, text=True, timeout=30,
        )
        if restart.returncode != 0:
            log.error("docker restart failed: %s", restart.stderr)

    except subprocess.TimeoutExpired:
        log.error("Self-update timed out")
    except FileNotFoundError:
        log.error("docker not found during self-update")


# ---------------------------------------------------------------------------
# Main reconciliation loop
# ---------------------------------------------------------------------------

def load_node_manifests(control_repo: Repo) -> list[AppManifest]:
    """Load all app manifests for this node from the control repo."""
    node_dir = Path(control_repo.working_dir) / "nodes" / GITOPS_NODE_NAME
    if not node_dir.exists():
        log.warning("No manifest directory found for node '%s' at %s", GITOPS_NODE_NAME, node_dir)
        return []

    manifests = []
    for manifest_file in sorted(node_dir.glob("*.yml")):
        try:
            manifest = parse_manifest(manifest_file)
            manifests.append(manifest)
            log.debug("Loaded manifest: %s (enabled=%s)", manifest.name, manifest.enabled)
        except (ValueError, yaml.YAMLError) as e:
            log.error("Skipping invalid manifest %s: %s", manifest_file.name, e)

    log.info("Loaded %d manifest(s) for node '%s'", len(manifests), GITOPS_NODE_NAME)
    return manifests


def reconcile_app(app: AppManifest) -> bool:
    """
    Reconcile a single app. Returns True on success.
    Clones stack repo if needed, syncs, runs compose if changed.
    """
    stack_path = STACKS_DIR / app.name

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
        return False

    # Check for changes and sync
    updated = sync_repo(repo, app.ref)

    if updated:
        log.info("App '%s' repo updated, running compose", app.name)
        return run_compose(app, stack_path)
    else:
        log.info("App '%s' is up to date, no action needed", app.name)
        return True


def reconcile(mode: str = "reconcile") -> int:
    """
    Main entry point for a reconciliation run.
    mode: 'reconcile' | 'self-update'
    Returns exit code (0 = success, 1 = partial failure, 2 = fatal).
    """
    if mode == "self-update":
        self_update()
        return 0

    if not CONTROL_REPO_URL:
        log.error("CONTROL_REPO_URL is not set")
        return 2

    GITOPS_ROOT.mkdir(parents=True, exist_ok=True)
    STACKS_DIR.mkdir(parents=True, exist_ok=True)

    log.info(
        "Starting reconciliation | node=%s root=%s",
        GITOPS_NODE_NAME,
        GITOPS_ROOT,
    )

    # Step 1: sync control repo
    try:
        control_repo = ensure_repo(
            url=CONTROL_REPO_URL,
            local_path=CONTROL_REPO_DIR,
            branch=CONTROL_REPO_BRANCH,
        )
    except GitCommandError as e:
        log.error("Failed to clone/open control repo: %s", e)
        return 2

    control_ref = AppRef(branch=CONTROL_REPO_BRANCH)
    sync_repo(control_repo, control_ref)

    # Step 2: load manifests for this node
    manifests = load_node_manifests(control_repo)
    if not manifests:
        log.info("No apps to reconcile")
        return 0

    # Step 3: reconcile each enabled app
    results = {}
    for app in manifests:
        if not app.enabled:
            log.info("App '%s' is disabled, skipping", app.name)
            results[app.name] = "disabled"
            continue
        success = reconcile_app(app)
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

    if failed:
        log.warning("Failed apps: %s", ", ".join(failed))
        return 1

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "reconcile"
    sys.exit(reconcile(mode=mode))
