from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from git import GitCommandError

import steward


def _write_manifest(tmp_path: Path, content: str) -> Path:
    file_path = tmp_path / "app.yml"
    file_path.write_text(content)
    return file_path


def _sample_manifest(**overrides) -> dict:
    data = {
        "version": 1,
        "name": "demo",
        "repo": "https://example.com/repo.git",
        "ref": {"branch": "main"},
        "enabled": True,
    }
    data.update(overrides)
    return data


def _to_yaml(data: dict) -> str:
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {sub_value}")
        else:
            rendered = str(value).lower() if isinstance(value, bool) else value
            lines.append(f"{key}: {rendered}")
    return "\n".join(lines) + "\n"


def test_parse_manifest_valid_branch(tmp_path: Path) -> None:
    manifest_file = _write_manifest(tmp_path, _to_yaml(_sample_manifest()))

    parsed = steward.parse_manifest(manifest_file)

    assert parsed.name == "demo"
    assert parsed.ref.branch == "main"
    assert parsed.ref.tag is None
    assert parsed.compose_file == "docker-compose.yml"
    assert parsed.path == "."


def test_parse_manifest_rejects_branch_and_tag(tmp_path: Path) -> None:
    manifest = _sample_manifest(ref={"branch": "main", "tag": "v1.0.0"})
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="either branch or tag, not both"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_requires_enabled(tmp_path: Path) -> None:
    manifest = _sample_manifest()
    manifest.pop("enabled")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="missing required field: enabled"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_rejects_unsupported_version(tmp_path: Path) -> None:
    manifest = _sample_manifest(version=2)
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="unsupported version"):
        steward.parse_manifest(manifest_file)


def test_sync_repo_returns_none_on_remote_sha_error(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"

    monkeypatch.setattr(steward, "get_remote_sha", lambda _repo, _ref: None)

    result = steward.sync_repo(repo, steward.AppRef(branch="main"))

    assert result is None


def test_sync_repo_returns_false_when_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"

    monkeypatch.setattr(steward, "get_remote_sha", lambda _repo, _ref: "abc123")

    result = steward.sync_repo(repo, steward.AppRef(branch="main"))

    assert result is False
    repo.remotes.origin.pull.assert_not_called()


def test_sync_repo_pulls_branch_when_remote_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"

    monkeypatch.setattr(steward, "get_remote_sha", lambda _repo, _ref: "def456")

    result = steward.sync_repo(repo, steward.AppRef(branch="main"))

    assert result is True
    repo.remotes.origin.pull.assert_called_once_with("main")


def test_sync_repo_checks_out_tag_when_remote_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"

    monkeypatch.setattr(steward, "get_remote_sha", lambda _repo, _ref: "def456")

    result = steward.sync_repo(repo, steward.AppRef(tag="v1.0.0"))

    assert result is True
    repo.git.checkout.assert_called_once_with("v1.0.0")


def test_sync_repo_returns_none_when_pull_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"
    repo.remotes.origin.pull.side_effect = GitCommandError("pull", 1, stderr="boom")

    monkeypatch.setattr(steward, "get_remote_sha", lambda _repo, _ref: "def456")

    result = steward.sync_repo(repo, steward.AppRef(branch="main"))

    assert result is None


def test_resolve_host_path_uses_mount_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        steward,
        "_find_best_mount",
        lambda _path: ({"Source": "/host/data"}, "stack/app"),
    )

    resolved = steward._resolve_host_path(Path("/git/stacks/app"))

    assert resolved == "/host/data/stack/app"


def test_resolve_host_path_returns_none_without_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steward, "_find_best_mount", lambda _path: ({}, ""))

    resolved = steward._resolve_host_path(Path("/git/stacks/app"))

    assert resolved is None


def test_inc_creates_nested_counter() -> None:
    state = {}

    steward._inc(state, "apps", "demo", "reconcile_total", "success")
    steward._inc(state, "apps", "demo", "reconcile_total", "success", by=2)

    assert state["apps"]["demo"]["reconcile_total"]["success"] == 3


def test_spawn_compose_helper_falls_back_when_helper_image_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=1,
        name="steward",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "_get_helper_image", lambda: None)

    fallback_called = {"value": False}

    def _fake_run_compose(_app, _stack_path):
        fallback_called["value"] = True
        return True

    monkeypatch.setattr(steward, "run_compose", _fake_run_compose)

    result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is True
    assert fallback_called["value"] is True


def test_spawn_compose_helper_falls_back_when_host_path_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=1,
        name="steward",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    def _fake_resolve_host_path(_path: Path):
        return None

    monkeypatch.setattr(steward, "_resolve_host_path", _fake_resolve_host_path)

    fallback_called = {"value": False}

    def _fake_run_compose(_app, _stack_path):
        fallback_called["value"] = True
        return True

    monkeypatch.setattr(steward, "run_compose", _fake_run_compose)

    result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is True
    assert fallback_called["value"] is True


def test_run_compose_uses_explicit_project_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    app = steward.AppManifest(
        version=1,
        name="demo",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    seen_cmd: list[str] = []

    def _fake_run(cmd, **_kwargs):
        seen_cmd[:] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(app, tmp_path)

    assert result is True
    assert "--project-name" in seen_cmd
    assert seen_cmd[seen_cmd.index("--project-name") + 1] == "demo"


def test_spawn_compose_helper_uses_explicit_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=1,
        name="steward",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward, "_resolve_host_path", lambda p: str(p))

    seen_helper_cmd: list[str] = []

    def _fake_run(cmd, **_kwargs):
        seen_helper_cmd[:] = cmd
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is True
    helper_shell = seen_helper_cmd[-1]
    assert "--project-name steward" in helper_shell


def test_check_app_returns_synced() -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"
    repo.remotes.origin.refs = {"main": MagicMock(commit=MagicMock(hexsha="abc123"))}

    result = steward.check_app(repo, steward.AppRef(branch="main"))

    assert result.status == steward.SyncStatus.SYNCED
    assert result.local_sha == "abc123"
    assert result.remote_sha == "abc123"


def test_check_app_returns_out_of_sync() -> None:
    repo = MagicMock()
    repo.head.commit.hexsha = "abc123"
    repo.remotes.origin.refs = {"main": MagicMock(commit=MagicMock(hexsha="def456"))}

    result = steward.check_app(repo, steward.AppRef(branch="main"))

    assert result.status == steward.SyncStatus.OUT_OF_SYNC
    assert result.local_sha == "abc123"
    assert result.remote_sha == "def456"


def test_sync_app_returns_git_apply_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=1,
        name="demo",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "apply_ref", lambda _repo, _ref: False)

    result = steward.sync_app(app, MagicMock(), Path("/tmp/demo"))

    assert result.success is False
    assert result.message == "git_apply_failed"


def test_sync_app_returns_success(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=1,
        name="demo",
        repo="https://example.com/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "apply_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(steward, "_is_self_update", lambda _app: False)
    monkeypatch.setattr(steward, "run_compose", lambda _app, _stack: True)

    result = steward.sync_app(app, MagicMock(), Path("/tmp/demo"))

    assert result.success is True
    assert result.message == "synced"


def test_sqlite_state_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steward, "DB_FILE", tmp_path / "steward.db")

    state = {
        "node": steward.GITOPS_NODE_NAME,
        "reconcile": {
            "last_timestamp": 100.0,
            "last_duration_seconds": 2.5,
            "total": {"success": 3, "partial_failure": 1, "fatal": 0},
            "control_repo_sync_total": {"up_to_date": 5, "updated": 2, "failed": 1},
            "manifest_parse_errors": 4,
        },
        "apps": {
            "demo": {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "ref_type": "branch",
                "enabled": True,
                "last_reconcile_timestamp": 101.0,
                "last_sync_timestamp": 102.0,
                "reconcile_total": {"success": 7, "failed": 2, "skipped": 1},
                "sync_total": {"success": 3, "failed": 1},
            }
        },
    }

    steward._save_metrics_state(state)
    loaded = steward._load_metrics_state()

    assert loaded["node"] == steward.GITOPS_NODE_NAME
    assert loaded["reconcile"]["total"]["success"] == 3
    assert loaded["reconcile"]["control_repo_sync_total"]["updated"] == 2
    assert loaded["apps"]["demo"]["reconcile_total"]["failed"] == 2
    assert loaded["apps"]["demo"]["sync_total"]["success"] == 3
