import json
import sqlite3
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
        "repo": "git@example.com:org/repo.git",
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
    manifest = _sample_manifest(version=3)
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="unsupported version"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_v2_supports_compose_env_and_policies(tmp_path: Path) -> None:
    manifest = _sample_manifest(
        version=2,
        compose_env_file="/git/envs/demo.env",
        sync_policy="manual",
        pull_policy="missing",
        notify_url="https://notify.example/hook",
    )
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    parsed = steward.parse_manifest(manifest_file)

    assert parsed.version == 2
    assert parsed.env_file == "/git/envs/demo.env"
    assert parsed.sync_policy == "manual"
    assert parsed.pull_policy == "missing"
    assert parsed.notify_url == "https://notify.example/hook"


def test_parse_manifest_rejects_both_env_keys(tmp_path: Path) -> None:
    manifest = _sample_manifest(
        version=2,
        compose_env_file="/git/envs/new.env",
        env_file="/git/envs/old.env",
    )
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="compose_env_file and env_file are mutually exclusive"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_defaults_policies_for_v1(tmp_path: Path) -> None:
    manifest_file = _write_manifest(tmp_path, _to_yaml(_sample_manifest()))

    parsed = steward.parse_manifest(manifest_file)

    assert parsed.sync_policy == "auto"
    assert parsed.pull_policy == "always"


def test_parse_manifest_rejects_invalid_pull_policy(tmp_path: Path) -> None:
    manifest = _sample_manifest(version=2, pull_policy="fast")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="unsupported pull_policy"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_rejects_invalid_sync_policy(tmp_path: Path) -> None:
    manifest = _sample_manifest(version=2, sync_policy="on_demand")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="unsupported sync_policy"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_accepts_plain_https_repo_url(tmp_path: Path) -> None:
    manifest = _sample_manifest(repo="https://github.com/you/repo.git")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))
    result = steward.parse_manifest(manifest_file)
    assert result.repo == "https://github.com/you/repo.git"


def test_parse_manifest_rejects_https_repo_url_with_credentials(tmp_path: Path) -> None:
    manifest = _sample_manifest(repo="https://oauth2:token@github.com/you/repo.git")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="embedded credentials"):
        steward.parse_manifest(manifest_file)


def test_is_ssh_url_accepts_valid_urls() -> None:
    assert steward.is_ssh_url("git@github.com:org/repo.git") is True
    assert steward.is_ssh_url("ssh://git@github.com/org/repo.git") is True
    assert steward.is_ssh_url("git@gitlab.com:org/repo.git") is True


def test_is_ssh_url_rejects_https() -> None:
    assert steward.is_ssh_url("https://github.com/org/repo.git") is False
    assert steward.is_ssh_url("https://oauth2:token@github.com/org/repo") is False
    assert steward.is_ssh_url("http://github.com/org/repo.git") is False


def test_validate_repo_url_returns_none_for_ssh() -> None:
    assert steward.validate_repo_url("git@github.com:org/repo.git") is None
    assert steward.validate_repo_url("ssh://git@github.com/org/repo.git") is None


def test_validate_repo_url_returns_none_for_plain_https() -> None:
    assert steward.validate_repo_url("https://github.com/org/repo.git") is None
    assert steward.validate_repo_url("http://github.com/org/repo.git") is None


def test_validate_repo_url_rejects_https_with_credentials() -> None:
    err = steward.validate_repo_url("https://oauth2:token@github.com/org/repo.git")
    assert err is not None
    assert "embedded credentials" in err


def test_strip_url_credentials_removes_userinfo() -> None:
    assert steward.strip_url_credentials("https://oauth2:secret@github.com/org/repo") == "https://github.com/org/repo"
    assert steward.strip_url_credentials("https://user:pass@gitlab.com/org/repo.git") == "https://gitlab.com/org/repo.git"


def test_strip_url_credentials_preserves_ssh_url() -> None:
    url = "git@github.com:org/repo.git"
    assert steward.strip_url_credentials(url) == url


# ---------------------------------------------------------------------------
# credentials.yml — parse_credentials_file
# ---------------------------------------------------------------------------

def test_parse_credentials_file_valid(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text(
        "credentials:\n"
        "  - pattern: github.com\n"
        "    key_file: /run/secrets/github_key\n"
        "  - pattern: gitlab.com\n"
        "    key_file: /run/secrets/gitlab_key\n"
        "known_hosts_file: /run/secrets/ssh_known_hosts\n"
    )

    cfg = steward.parse_credentials_file(str(creds_file))

    assert len(cfg.credentials) == 2
    assert cfg.credentials[0].pattern == "github.com"
    assert cfg.credentials[0].key_file == "/run/secrets/github_key"
    assert cfg.credentials[1].pattern == "gitlab.com"
    assert cfg.credentials[1].key_file == "/run/secrets/gitlab_key"
    assert cfg.known_hosts_file == "/run/secrets/ssh_known_hosts"


def test_parse_credentials_file_without_known_hosts(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text(
        "credentials:\n"
        "  - pattern: github.com\n"
        "    key_file: /run/secrets/github_key\n"
    )

    cfg = steward.parse_credentials_file(str(creds_file))

    assert cfg.known_hosts_file is None


def test_parse_credentials_file_rejects_missing_pattern(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text(
        "credentials:\n"
        "  - key_file: /run/secrets/github_key\n"
    )

    with pytest.raises(ValueError, match="pattern"):
        steward.parse_credentials_file(str(creds_file))


def test_parse_credentials_file_rejects_missing_key_file(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text(
        "credentials:\n"
        "  - pattern: github.com\n"
    )

    with pytest.raises(ValueError, match="key_file"):
        steward.parse_credentials_file(str(creds_file))


def test_parse_credentials_file_rejects_non_list_credentials(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text("credentials: not-a-list\n")

    with pytest.raises(ValueError, match="list"):
        steward.parse_credentials_file(str(creds_file))


def test_parse_credentials_file_warns_on_path_component_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text(
        "credentials:\n"
        "  - pattern: github.com/myorg\n"
        "    key_file: /run/secrets/org_key\n"
    )

    warnings = []
    monkeypatch.setattr(steward.log, "warning", lambda msg, *args: warnings.append(msg % args))

    cfg = steward.parse_credentials_file(str(creds_file))

    assert len(cfg.credentials) == 1
    assert any("path components" in w for w in warnings)


# ---------------------------------------------------------------------------
# credentials.yml — generate_ssh_config
# ---------------------------------------------------------------------------

def test_generate_ssh_config_two_keys() -> None:
    cfg = steward.CredentialsConfig(
        credentials=[
            steward.CredentialEntry(pattern="github.com", key_file="/run/secrets/github_key"),
            steward.CredentialEntry(pattern="gitlab.com", key_file="/run/secrets/gitlab_key"),
        ]
    )

    text = steward.generate_ssh_config(cfg)

    assert "Host github.com" in text
    assert "IdentityFile /run/secrets/github_key" in text
    assert "Host gitlab.com" in text
    assert "IdentityFile /run/secrets/gitlab_key" in text
    assert "IdentitiesOnly yes" in text


def test_generate_ssh_config_with_known_hosts() -> None:
    cfg = steward.CredentialsConfig(
        credentials=[
            steward.CredentialEntry(pattern="github.com", key_file="/run/secrets/github_key"),
        ],
        known_hosts_file="/run/secrets/ssh_known_hosts",
    )

    text = steward.generate_ssh_config(cfg)

    assert "UserKnownHostsFile /run/secrets/ssh_known_hosts" in text


def test_generate_ssh_config_strict_host_key_checking_yes_when_known_hosts_set() -> None:
    cfg = steward.CredentialsConfig(
        credentials=[
            steward.CredentialEntry(pattern="github.com", key_file="/run/secrets/github_key"),
        ],
        known_hosts_file="/run/secrets/ssh_known_hosts",
    )

    text = steward.generate_ssh_config(cfg, strict_host_key_checking="yes")

    assert "StrictHostKeyChecking yes" in text


def test_generate_ssh_config_host_strips_path_components() -> None:
    cfg = steward.CredentialsConfig(
        credentials=[
            steward.CredentialEntry(pattern="github.com/myorg", key_file="/run/secrets/org_key"),
        ]
    )

    text = steward.generate_ssh_config(cfg)

    assert "Host github.com" in text
    assert "Host github.com/myorg" not in text


def test_generate_ssh_config_wildcard_fallback() -> None:
    cfg = steward.CredentialsConfig(
        credentials=[
            steward.CredentialEntry(pattern="*", key_file="/run/secrets/default_key"),
        ]
    )

    text = steward.generate_ssh_config(cfg)

    assert "Host *" in text
    assert "IdentityFile /run/secrets/default_key" in text


def test_fetch_ref_sanitizes_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.git.fetch.side_effect = GitCommandError(
        "fetch", 128, stderr="fatal: repository 'https://oauth2:s3cr3t@github.com/org/repo.git' not found"
    )

    log_messages = []
    monkeypatch.setattr(steward.log, "error", lambda msg, *args: log_messages.append(msg % args))

    result = steward.fetch_ref(repo, steward.AppRef(branch="main"))

    assert result is False
    assert len(log_messages) > 0
    assert all("s3cr3t" not in m for m in log_messages)


def test_apply_ref_sanitizes_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.git.pull.side_effect = GitCommandError(
        "pull", 128, stderr="ERROR: https://oauth2:t0ken@github.com/org/repo.git permission denied"
    )

    log_messages = []
    monkeypatch.setattr(steward.log, "error", lambda msg, *args: log_messages.append(msg % args))

    result = steward.apply_ref(repo, steward.AppRef(branch="main"))

    assert result is False
    assert len(log_messages) > 0
    assert all("t0ken" not in m for m in log_messages)


def test_ensure_repo_clone_log_sanitizes_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cloned_path = tmp_path / "repo"

    log_messages = []
    monkeypatch.setattr(steward.log, "info", lambda msg, *args: log_messages.append(msg % args))

    fake_repo = MagicMock()
    monkeypatch.setattr(steward.Repo, "clone_from", lambda url, path, **kwargs: fake_repo)

    steward.ensure_repo(
        url="https://oauth2:mysecret@github.com/org/repo.git",
        local_path=cloned_path,
    )

    assert all("mysecret" not in m for m in log_messages)
    assert any("github.com/org/repo.git" in m for m in log_messages)


def test_parse_manifest_defaults_health_delay(tmp_path: Path) -> None:
    manifest_file = _write_manifest(tmp_path, _to_yaml(_sample_manifest()))

    parsed = steward.parse_manifest(manifest_file)

    assert parsed.health_check_delay_seconds == 30


def test_parse_manifest_rejects_invalid_health_delay(tmp_path: Path) -> None:
    manifest = _sample_manifest(version=2, health_check_delay_seconds=-1)
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="health_check_delay_seconds must be >= 0"):
        steward.parse_manifest(manifest_file)


def test_parse_manifest_rejects_non_integer_health_delay(tmp_path: Path) -> None:
    manifest = _sample_manifest(version=2, health_check_delay_seconds="fast")
    manifest_file = _write_manifest(tmp_path, _to_yaml(manifest))

    with pytest.raises(ValueError, match="health_check_delay_seconds must be an integer"):
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
    repo.git.pull.assert_called_once_with("origin", "main")


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
    repo.git.pull.side_effect = GitCommandError("pull", 1, stderr="boom")

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
        repo="git@example.com:org/repo.git",
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
        repo="git@example.com:org/repo.git",
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
        repo="git@example.com:org/repo.git",
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


def test_run_compose_uses_manifest_pull_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        pull_policy="missing",
    )

    seen_cmd: list[str] = []

    def _fake_run(cmd, **_kwargs):
        seen_cmd[:] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(app, tmp_path)

    assert result is True
    assert "--pull" in seen_cmd
    assert seen_cmd[seen_cmd.index("--pull") + 1] == "missing"


def test_reconcile_app_manual_sync_skips_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="manual",
    )

    state: dict = {}
    repo = MagicMock()
    repo.working_dir = "/tmp/repo"

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.OUT_OF_SYNC,
            local_sha="abc123",
            remote_sha="def456",
        ),
    )

    sync_called = {"value": False}

    def _fake_sync_app(_app, _repo, _path):
        sync_called["value"] = True
        return steward.SyncResult(success=True, message="synced")

    monkeypatch.setattr(steward, "sync_app", _fake_sync_app)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert sync_called["value"] is False
    assert state["apps"]["demo"]["sync_status"] == "OutOfSync"
    assert state["apps"]["demo"]["reconcile_total"]["skipped"] == 1
    assert "sync_total" not in state["apps"]["demo"]


def test_reconcile_app_auto_sync_applies_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()
    repo.working_dir = "/tmp/repo"

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.OUT_OF_SYNC,
            local_sha="abc123",
            remote_sha="def456",
        ),
    )

    sync_called = {"value": False}

    def _fake_sync_app(_app, _repo, _path):
        sync_called["value"] = True
        return steward.SyncResult(success=True, message="synced")

    monkeypatch.setattr(steward, "sync_app", _fake_sync_app)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert sync_called["value"] is True
    assert state["apps"]["demo"]["sync_status"] == "Synced"
    assert state["apps"]["demo"]["health_status"] == "Progressing"
    assert state["apps"]["demo"]["sync_total"]["success"] == 1


def test_reconcile_app_sets_synced_status_when_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(steward, "_detect_live_drift", lambda _app, _stack: (False, "no_drift"))
    monkeypatch.setattr(steward, "_evaluate_health_status", lambda _app, _path, _state: "Healthy")

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["sync_status"] == "Synced"
    assert state["apps"]["demo"]["health_status"] == "Healthy"


def test_reconcile_app_sets_unknown_status_on_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: False)

    result = steward.reconcile_app(app, state)

    assert result is False
    assert state["apps"]["demo"]["sync_status"] == "Unknown"
    assert state["apps"]["demo"]["health_status"] == "Unknown"


def test_sqlite_state_roundtrip_includes_sync_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steward, "DB_FILE", tmp_path / "steward.db")

    state = {
        "node": steward.GITOPS_NODE_NAME,
        "apps": {
            "demo": {
                "repo": "git@example.com:org/repo.git",
                "ref": "main",
                "ref_type": "branch",
                "enabled": True,
                "sync_policy": "manual",
                "sync_status": "OutOfSync",
                "health_status": "Degraded",
                "deployed_sha": "abc123",
                "remote_sha": "def456",
                "reconcile_total": {"success": 0, "failed": 0, "skipped": 1},
                "sync_total": {"success": 0, "failed": 0},
            }
        },
    }

    steward._save_metrics_state(state)
    loaded = steward._load_metrics_state()

    assert loaded["apps"]["demo"]["sync_status"] == "OutOfSync"
    assert loaded["apps"]["demo"]["health_status"] == "Degraded"
    assert loaded["apps"]["demo"]["deployed_sha"] == "abc123"
    assert loaded["apps"]["demo"]["remote_sha"] == "def456"


def test_classify_health_status_running_is_healthy() -> None:
    status = steward._classify_health_status([{"State": "running"}])
    assert status == "Healthy"


def test_classify_health_status_restarting_is_degraded() -> None:
    status = steward._classify_health_status([{"State": "restarting"}])
    assert status == "Degraded"


def test_classify_health_status_oneshot_exit_zero_is_healthy() -> None:
    status = steward._classify_health_status([{"State": "exited", "ExitCode": 0}])
    assert status == "Healthy"


def test_evaluate_health_status_progressing_when_delay_not_elapsed() -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        health_check_delay_seconds=30,
    )
    app_state = {"last_sync_timestamp": steward.time.time()}

    status = steward._evaluate_health_status(app, Path("/tmp/demo"), app_state)

    assert status == "Progressing"


def test_evaluate_health_status_degraded_manual_no_auto_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="manual",
        health_check_delay_seconds=0,
    )
    app_state = {"last_sync_timestamp": steward.time.time() - 60}

    monkeypatch.setattr(
        steward,
        "_load_compose_services_status",
        lambda _app, _stack: [{"State": "restarting"}],
    )

    run_called = {"value": False}

    def _fake_run_compose(_app, _stack):
        run_called["value"] = True
        return True

    monkeypatch.setattr(steward, "run_compose", _fake_run_compose)

    status = steward._evaluate_health_status(app, Path("/tmp/demo"), app_state)

    assert status == "Degraded"
    assert run_called["value"] is False


def test_evaluate_health_status_degraded_auto_attempts_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
        health_check_delay_seconds=0,
    )
    app_state = {"last_sync_timestamp": steward.time.time() - 60}

    monkeypatch.setattr(
        steward,
        "_load_compose_services_status",
        lambda _app, _stack: [{"State": "restarting"}],
    )
    status = steward._evaluate_health_status(app, Path("/tmp/demo"), app_state)

    assert status == "Degraded"


def test_detect_live_drift_missing_service(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "_load_expected_services", lambda _app, _stack: {"web", "db"})
    monkeypatch.setattr(
        steward,
        "_load_compose_services_status",
        lambda _app, _stack: [{"Service": "web", "State": "running"}],
    )

    drifted, reason = steward._detect_live_drift(app, Path("/tmp/demo"))

    assert drifted is True
    assert "db:missing" in reason


def test_reconcile_app_synced_drift_manual_logs_skipped_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="manual",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(steward, "_detect_live_drift", lambda _app, _stack: (True, "live_drift_detected[db:missing]"))
    monkeypatch.setattr(steward, "_evaluate_health_status", lambda _app, _stack, _state: "Degraded")

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(steward, "_send_notification", lambda _app, event, payload: sent.append((event, payload)))

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["sync_status"] == "OutOfSync"
    assert state["apps"]["demo"]["reconcile_total"]["skipped"] == 1
    assert state["_operations"][0]["sync_status"] == "Skipped"
    assert sent[0][0] == "drift_detected"


def test_reconcile_app_synced_drift_auto_self_heals(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(steward, "_detect_live_drift", lambda _app, _stack: (True, "live_drift_detected[db:missing]"))
    monkeypatch.setattr(steward, "run_compose", lambda _app, _stack: True)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["sync_status"] == "Synced"
    assert state["apps"]["demo"]["health_status"] == "Progressing"
    assert state["apps"]["demo"]["sync_total"]["success"] == 1
    assert state["_operations"][0]["trigger"] == "self_heal"


def test_reconcile_app_synced_expected_services_unavailable_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(
        steward, "_detect_live_drift", lambda _app, _stack: (False, "expected_services_unavailable")
    )

    result = steward.reconcile_app(app, state)

    assert result is False
    assert state["apps"]["demo"]["sync_status"] == "Unknown"
    assert state["apps"]["demo"]["health_status"] == "Unknown"
    assert state["apps"]["demo"]["reconcile_total"]["failed"] == 1


def test_reconcile_app_synced_live_state_unavailable_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(steward, "_detect_live_drift", lambda _app, _stack: (False, "live_state_unavailable"))

    result = steward.reconcile_app(app, state)

    assert result is False
    assert state["apps"]["demo"]["sync_status"] == "Unknown"
    assert state["apps"]["demo"]["health_status"] == "Unknown"
    assert state["apps"]["demo"]["reconcile_total"]["failed"] == 1


def test_sync_failure_sends_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()
    repo.working_dir = "/tmp/repo"

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.OUT_OF_SYNC,
            local_sha="abc123",
            remote_sha="def456",
        ),
    )
    monkeypatch.setattr(steward, "sync_app", lambda _app, _repo, _path: steward.SyncResult(success=False, message="compose_failed"))

    sent: list[str] = []
    monkeypatch.setattr(steward, "_send_notification", lambda _app, event, payload: sent.append(event))

    result = steward.reconcile_app(app, state)

    assert result is False
    assert "sync_failed" in sent


def test_spawn_compose_helper_uses_explicit_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=1,
        name="steward",
        repo="git@example.com:org/repo.git",
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
        repo="git@example.com:org/repo.git",
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
        repo="git@example.com:org/repo.git",
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
                "repo": "git@example.com:org/repo.git",
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


def test_reconcile_sets_disabled_sync_status(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled_app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=False,
        source_file=Path("/tmp/app.yml"),
    )

    class _FakeRepo:
        working_dir = "/tmp/control"

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})

    saved: dict = {}

    def _fake_save(state: dict) -> None:
        saved.update(state)

    monkeypatch.setattr(steward, "_save_metrics_state", _fake_save)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([disabled_app], []))
    monkeypatch.setattr(steward, "_write_status_snapshot", lambda _repo, _state: True)

    result = steward.reconcile()

    assert result == 0
    assert saved["apps"]["demo"]["sync_status"] == "Disabled"


def test_reconcile_returns_partial_failure_when_status_writeback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=False,
        source_file=Path("/tmp/app.yml"),
    )

    class _FakeRepo:
        working_dir = "/tmp/control"

    saved: dict = {}

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})
    monkeypatch.setattr(steward, "_save_metrics_state", lambda state: saved.update(state))
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([disabled_app], []))
    monkeypatch.setattr(steward, "_write_status_snapshot", lambda _repo, _state: False)

    result = steward.reconcile()

    assert result == 1
    assert saved["reconcile"]["total"]["partial_failure"] == 1


def test_write_status_snapshot_commits_only_when_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "control"
    repo = MagicMock()
    repo.working_dir = str(working_dir)

    monkeypatch.setattr(steward, "GITOPS_NODE_NAME", "node-a")
    monkeypatch.setattr(steward, "CONTROL_REPO_BRANCH", "main")

    state = {
        "apps": {
            "demo": {
                "sync_status": "Synced",
                "health_status": "Healthy",
                "deployed_sha": "abc",
                "remote_sha": "abc",
            }
        }
    }

    assert steward._write_status_snapshot(repo, state) is True
    assert repo.index.commit.call_count == 1
    assert repo.git.push.call_count == 1

    assert steward._write_status_snapshot(repo, state) is True
    assert repo.index.commit.call_count == 1
    assert repo.git.push.call_count == 1

    status_file = working_dir / "nodes" / "node-a" / "status.json"
    payload = json.loads(status_file.read_text())
    assert payload["node"] == "node-a"
    assert payload["apps"]["demo"]["sync_status"] == "Synced"


def test_reconcile_app_auto_sync_respects_global_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "STEWARD_DRY_RUN", True)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.OUT_OF_SYNC,
            local_sha="abc123",
            remote_sha="def456",
        ),
    )

    sync_called = {"value": False}

    def _fake_sync_app(_app, _repo, _path):
        sync_called["value"] = True
        return steward.SyncResult(success=True, message="synced")

    monkeypatch.setattr(steward, "sync_app", _fake_sync_app)
    monkeypatch.setattr(steward, "_evaluate_health_status", lambda _app, _path, _state: "Degraded")

    result = steward.reconcile_app(app, state)

    assert result is True
    assert sync_called["value"] is False
    assert state["apps"]["demo"]["sync_status"] == "OutOfSync"
    assert state["_operations"][0]["sync_status"] == "Skipped"


def test_reconcile_app_synced_drift_auto_self_heal_increments_oob_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=None,
        enabled=True,
        source_file=Path("/tmp/app.yml"),
        sync_policy="auto",
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "STEWARD_DRY_RUN", False)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward,
        "check_app",
        lambda _repo, _ref: steward.CheckResult(
            status=steward.SyncStatus.SYNCED,
            local_sha="abc123",
            remote_sha="abc123",
        ),
    )
    monkeypatch.setattr(steward, "_detect_live_drift", lambda _app, _stack: (True, "live_drift_detected[db:missing]"))
    monkeypatch.setattr(steward, "run_compose", lambda _app, _stack: True)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["ooband_heal_total"] == 1


def test_operation_retention_prunes_old_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steward, "DB_FILE", tmp_path / "steward.db")

    old_state = {
        "node": steward.GITOPS_NODE_NAME,
        "apps": {},
        "_operations": [
            {
                "app": "demo",
                "node": steward.GITOPS_NODE_NAME,
                "started_at": "2000-01-01T00:00:00Z",
                "completed_at": "2000-01-01T00:00:00Z",
                "trigger": "git_change",
                "from_sha": "a",
                "to_sha": "b",
                "sync_status": "Failed",
                "health_status": "Unknown",
                "duration_s": 0.1,
                "message": "old",
            }
        ],
    }

    new_state = {
        "node": steward.GITOPS_NODE_NAME,
        "apps": {},
        "_operations": [
            {
                "app": "demo",
                "node": steward.GITOPS_NODE_NAME,
                "started_at": "2099-01-01T00:00:00Z",
                "completed_at": "2099-01-01T00:00:00Z",
                "trigger": "git_change",
                "from_sha": "b",
                "to_sha": "c",
                "sync_status": "Synced",
                "health_status": "Healthy",
                "duration_s": 0.1,
                "message": "new",
            }
        ],
    }

    steward._save_metrics_state(old_state)
    steward._save_metrics_state(new_state)

    conn = sqlite3.connect(steward.DB_FILE)
    rows = conn.execute("SELECT started_at, message FROM operations ORDER BY id").fetchall()
    conn.close()

    assert rows == [("2099-01-01T00:00:00Z", "new")]


# ---------------------------------------------------------------------------
# Goal 7 — parse-error visibility in reconcile metrics
# ---------------------------------------------------------------------------

def test_reconcile_parse_error_app_appears_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest that fails to parse is recorded as 'failed' in results and metrics."""

    class _FakeRepo:
        working_dir = "/tmp/control"

    saved: dict = {}

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})
    monkeypatch.setattr(steward, "_save_metrics_state", lambda state: saved.update(state))
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(
        steward,
        "load_node_manifests",
        lambda _repo: ([], [("steward.yml", "steward", "repo: only SSH URLs are supported")]),
    )
    monkeypatch.setattr(steward, "_write_status_snapshot", lambda _repo, _state: True)

    result = steward.reconcile()

    assert result == 1
    assert saved["apps"]["steward"]["sync_status"] == steward.SyncStatus.UNKNOWN.value
    assert saved["apps"]["steward"]["health_status"] == steward.HEALTH_STATUS_UNKNOWN
    assert saved["apps"]["steward"]["reconcile_total"]["failed"] >= 1
    assert saved["reconcile"]["total"]["partial_failure"] == 1
    assert saved["reconcile"]["manifest_parse_errors"] == 1


def test_reconcile_parse_error_run_result_is_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with only parse errors records partial_failure, not success."""

    class _FakeRepo:
        working_dir = "/tmp/control"

    saved: dict = {}

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})
    monkeypatch.setattr(steward, "_save_metrics_state", lambda state: saved.update(state))
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(
        steward,
        "load_node_manifests",
        lambda _repo: (
            [],
            [
                ("app1.yml", "app1", "missing required field 'repo'"),
                ("app2.yml", "app2", "invalid URL scheme"),
            ],
        ),
    )
    monkeypatch.setattr(steward, "_write_status_snapshot", lambda _repo, _state: True)

    result = steward.reconcile()

    assert result == 1
    assert "success" not in saved.get("reconcile", {}).get("total", {})
    assert saved["reconcile"]["total"]["partial_failure"] == 1
    assert saved["reconcile"]["manifest_parse_errors"] == 2
