from pathlib import Path
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
