import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from git import GitCommandError, Repo

import state_store
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


def _stub_resolve_host_path(mapping: dict[str, str]):
    """Build a fake _resolve_host_path that maps known container paths by str()."""

    def _fake(container_path: Path):
        return mapping.get(str(container_path))

    return _fake


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
    assert (
        steward.strip_url_credentials("https://oauth2:secret@github.com/org/repo")
        == "https://github.com/org/repo"
    )
    assert (
        steward.strip_url_credentials("https://user:pass@gitlab.com/org/repo.git")
        == "https://gitlab.com/org/repo.git"
    )


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
        "credentials:\n  - pattern: github.com\n    key_file: /run/secrets/github_key\n"
    )

    cfg = steward.parse_credentials_file(str(creds_file))

    assert cfg.known_hosts_file is None


def test_parse_credentials_file_rejects_missing_pattern(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text("credentials:\n  - key_file: /run/secrets/github_key\n")

    with pytest.raises(ValueError, match="pattern"):
        steward.parse_credentials_file(str(creds_file))


def test_parse_credentials_file_rejects_missing_key_file(tmp_path: Path) -> None:
    creds_file = tmp_path / "credentials.yml"
    creds_file.write_text("credentials:\n  - pattern: github.com\n")

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
        "credentials:\n  - pattern: github.com/myorg\n    key_file: /run/secrets/org_key\n"
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
        "fetch",
        128,
        stderr="fatal: repository 'https://oauth2:s3cr3t@github.com/org/repo.git' not found",
    )

    log_messages = []
    monkeypatch.setattr(steward.log, "error", lambda msg, *args: log_messages.append(msg % args))

    result = steward.fetch_ref(repo, steward.AppRef(branch="main"))

    assert result is False
    assert len(log_messages) > 0
    assert all("s3cr3t" not in m for m in log_messages)


def test_apply_ref_sanitizes_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MagicMock()
    repo.git.fetch.side_effect = GitCommandError(
        "fetch", 128, stderr="ERROR: https://oauth2:t0ken@github.com/org/repo.git permission denied"
    )

    log_messages = []
    monkeypatch.setattr(steward.log, "error", lambda msg, *args: log_messages.append(msg % args))

    result = steward.apply_ref(repo, steward.AppRef(branch="main"))

    assert result is False
    assert len(log_messages) > 0
    assert all("t0ken" not in m for m in log_messages)


def test_ensure_repo_clone_log_sanitizes_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    repo.git.fetch.assert_any_call("origin", "main")
    repo.git.merge.assert_called_once_with("--ff-only", "origin/main")


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
    repo.git.fetch.side_effect = GitCommandError("fetch", 1, stderr="boom")

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


def test_resolve_host_path_returns_none_without_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # tmpfs / anonymous mounts report an empty Source; None must be trustworthy
    # for the strict peer-compose resolution added in run_compose().
    monkeypatch.setattr(
        steward,
        "_find_best_mount",
        lambda _path: ({"Source": "", "Destination": "/git"}, "stacks/app"),
    )

    resolved = steward._resolve_host_path(Path("/git/stacks/app"))

    assert resolved is None


def test_host_path_reports_no_host_source_for_sourceless_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        steward,
        "_find_best_mount",
        lambda _path: ({"Source": "", "Destination": "/git", "Name": "myvol"}, "stacks/app"),
    )

    result = steward.host_path(Path("/git/stacks/app"))

    assert result == "<no host source>  [volume: myvol]"


def test_find_best_mount_does_not_match_sibling_prefix() -> None:
    mounts = [
        {"Source": "/host/gitops", "Destination": "/opt/gitops"},
    ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(steward, "_container_mounts", lambda: mounts)
        best, rel = steward._find_best_mount(Path("/opt/gitopsdata/x.env"))

    assert best == {}
    assert rel == ""


def test_find_best_mount_matches_root_mount() -> None:
    mounts = [
        {"Source": "/host/root", "Destination": "/"},
    ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(steward, "_container_mounts", lambda: mounts)
        best, rel = steward._find_best_mount(Path("/git/stacks/app"))

    assert best == mounts[0]
    assert rel == "git/stacks/app"


def test_find_best_mount_prefers_longest_nested_destination() -> None:
    mounts = [
        {"Source": "/host/git", "Destination": "/git"},
        {"Source": "/host/arr-env", "Destination": "/git/stacks/arr"},
    ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(steward, "_container_mounts", lambda: mounts)
        best, rel = steward._find_best_mount(Path("/git/stacks/arr/.env"))

    assert best == mounts[1]
    assert rel == ".env"


def test_is_under() -> None:
    assert steward._is_under("/git", "/git") is True
    assert steward._is_under("/git/stacks/app", "/git") is True
    assert steward._is_under("/gitopsdata/x.env", "/git") is False
    assert steward._is_under("/opt/gitopsdata/x.env", "/opt/gitops") is False
    assert steward._is_under("/opt/gitops/x.env", "/opt/gitops") is True


def test_log_compose_path_mode_warns_when_host_root_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(steward, "AGENT_CONTAINER_NAME", "test-steward")
    monkeypatch.setattr(steward, "_resolve_host_path", lambda _path: None)

    with caplog.at_level("WARNING"):
        steward._log_compose_path_mode()

    assert any(record.levelname == "WARNING" for record in caplog.records)
    assert "test-steward" in caplog.text
    assert "AGENT_CONTAINER_NAME is currently 'test-steward'" in caplog.text
    assert "set AGENT_CONTAINER_NAME to the real container name" in caplog.text
    assert "relative bind mounts cannot be guaranteed" in caplog.text


def test_log_compose_path_mode_logs_peer_mode_when_paths_differ(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(steward, "_resolve_host_path", lambda _path: "/host/git")

    with caplog.at_level("INFO"):
        steward._log_compose_path_mode()

    assert any(record.levelname == "INFO" for record in caplog.records)
    assert str(steward.GITOPS_ROOT) in caplog.text
    assert "/host/git" in caplog.text
    assert "peer helper" in caplog.text


def test_log_compose_path_mode_logs_direct_mode_when_paths_match(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(steward, "_resolve_host_path", lambda _path: str(steward.GITOPS_ROOT))

    with caplog.at_level("DEBUG"):
        steward._log_compose_path_mode()

    assert any(record.levelname == "DEBUG" for record in caplog.records)
    assert "direct compose is used" in caplog.text
    assert "more-specific mount" in caplog.text


def test_log_compose_path_mode_does_not_raise_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise(_path):
        raise RuntimeError("inspect unavailable")

    monkeypatch.setattr(steward, "_resolve_host_path", _raise)

    with caplog.at_level("WARNING"):
        steward._log_compose_path_mode()

    assert "Compose path mode check failed" in caplog.text
    assert "inspect unavailable" in caplog.text


def test_reconcile_calls_compose_path_mode_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "git"
    monkeypatch.setattr(steward, "GITOPS_ROOT", root)
    monkeypatch.setattr(steward, "STACKS_DIR", root / "stacks")
    monkeypatch.setattr(steward, "CONTROL_REPO_DIR", root / "control")
    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "https://example.com/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {})
    monkeypatch.setattr(steward, "_save_metrics_state", lambda _state: None)
    monkeypatch.setattr(steward, "_maybe_warn_legacy_json_state", lambda: None)
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "log_mounts", lambda: None)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([], []))
    calls: list[str] = []
    monkeypatch.setattr(steward, "_log_compose_path_mode", lambda: calls.append("guard"))

    result = steward.reconcile()

    assert result == 0
    assert calls == ["guard"]


def _demo_app(**overrides) -> steward.AppManifest:
    fields = {
        "version": 2,
        "name": "demo",
        "repo": "git@example.com:org/repo.git",
        "ref": steward.AppRef(branch="main"),
        "path": ".",
        "compose_file": "docker-compose.yml",
        "env_file": None,
        "enabled": True,
        "source_file": Path("/tmp/app.yml"),
    }
    fields.update(overrides)
    return steward.AppManifest(**fields)


def _pin_direct_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make run_compose select its direct compatibility path explicitly."""
    monkeypatch.setattr(steward, "_resolve_host_path", lambda path: str(path))


def test_compose_files_returns_main_file_only_when_no_override(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    files = steward._compose_files(_demo_app(), tmp_path)

    assert files == [str(compose_file)]


def test_compose_files_includes_override_when_present(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")

    files = steward._compose_files(_demo_app(), tmp_path)

    assert files == [str(compose_file), str(override_file)]


def test_compose_file_args_interleaves_dashf(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")

    args = steward._compose_file_args(_demo_app(), tmp_path)

    assert args == ["-f", str(compose_file), "-f", str(override_file)]


def test_resolve_compose_host_paths_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert reason == ""
    assert paths is not None
    assert paths.host_root == "/host/git"
    assert paths.host_workdir == "/host/git"
    assert paths.compose_files == ["/host/git/docker-compose.yml"]
    assert paths.env_file is None
    assert paths.bind_specs == ["/host/git:/host/git"]


def test_resolve_compose_host_paths_fails_when_host_root_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(steward, "_resolve_host_path", lambda _p: None)

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert paths is None
    assert reason == "host_root"


def test_resolve_compose_host_paths_fails_when_workdir_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path({str(tmp_path): "/host/git"}),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(path="sub"), tmp_path)

    assert paths is None
    assert reason == "workdir"


def test_resolve_compose_host_paths_fails_when_compose_file_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert paths is None
    assert reason == "compose_file"


def test_resolve_compose_host_paths_fails_when_override_present_but_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
                # override_file deliberately absent -> unresolvable
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert paths is None
    assert reason == "override"


def test_resolve_compose_host_paths_fails_when_env_file_missing_in_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
            }
        ),
    )

    app = _demo_app(env_file=str(tmp_path / "missing.env"))
    paths, reason = steward._resolve_compose_host_paths(app, tmp_path)

    assert paths is None
    assert reason == "env_file"


def test_resolve_compose_host_paths_fails_when_env_file_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    env_file = tmp_path / "app.env"
    env_file.write_text("FOO=bar\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
                # env_file deliberately absent -> unresolvable
            }
        ),
    )

    app = _demo_app(env_file=str(env_file))
    paths, reason = steward._resolve_compose_host_paths(app, tmp_path)

    assert paths is None
    assert reason == "env_file"


def test_resolve_compose_host_paths_adds_ro_bind_for_env_file_on_separate_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    env_file = tmp_path / "app.env"
    env_file.write_text("FOO=bar\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
                str(env_file): "/opt/secrets/app.env",
            }
        ),
    )

    app = _demo_app(env_file=str(env_file))
    paths, reason = steward._resolve_compose_host_paths(app, tmp_path)

    assert reason == ""
    assert paths is not None
    assert paths.env_file == "/opt/secrets/app.env"
    assert "/opt/secrets/app.env:/opt/secrets/app.env:ro" in paths.bind_specs
    assert paths.bind_specs.count("/opt/secrets/app.env:/opt/secrets/app.env:ro") == 1


def test_resolve_compose_host_paths_preserves_implicit_project_env_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    project_env = tmp_path / ".env"
    project_env.write_text("IMAGE_TAG=stable\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                str(project_env): "/opt/secrets/demo.env",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert reason == ""
    assert paths is not None
    assert paths.env_file is None
    assert paths.bind_specs.count("/opt/secrets/demo.env:/home/u/git/stacks/demo/.env:ro") == 1


def test_resolve_compose_host_paths_does_not_bind_implicit_project_env_when_same_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    project_env = tmp_path / ".env"
    project_env.write_text("IMAGE_TAG=stable\n")
    host_workdir = "/home/u/git/stacks/demo"
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): host_workdir,
                str(compose_file): f"{host_workdir}/docker-compose.yml",
                str(project_env): f"{host_workdir}/.env",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert reason == ""
    assert paths is not None
    assert paths.env_file is None
    assert not any(spec.endswith(":/home/u/git/stacks/demo/.env:ro") for spec in paths.bind_specs)


def test_resolve_compose_host_paths_rejects_unresolvable_implicit_project_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    project_env = tmp_path / ".env"
    project_env.write_text("IMAGE_TAG=stable\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                # project_env deliberately absent
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert paths is None
    assert reason == "project_env"


def test_resolve_compose_host_paths_no_redundant_ro_bind_for_file_under_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                # compose file resolves under host_root itself
                str(compose_file): "/host/git/docker-compose.yml",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert reason == ""
    assert paths is not None
    assert paths.bind_specs == ["/host/git:/host/git"]


def test_resolve_compose_host_paths_dedupes_bind_specs_when_root_equals_workdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    monkeypatch.setattr(steward, "GITOPS_ROOT", tmp_path)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(tmp_path): "/host/git",
                str(tmp_path / "."): "/host/git",
                str(compose_file): "/host/git/docker-compose.yml",
            }
        ),
    )

    paths, _reason = steward._resolve_compose_host_paths(_demo_app(), tmp_path)

    assert paths is not None
    assert paths.bind_specs.count("/host/git:/host/git") == 1


def test_peer_env_args_forwards_vars_and_excludes_docker_and_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        steward.os,
        "environ",
        {
            "GITOPS_NODE_NAME": "infra-1",
            "DOCKER_HOST": "tcp://should-not-appear",
            "HOME": "/root",
        },
    )

    args = steward._peer_env_args()

    assert "-e" in args
    assert "GITOPS_NODE_NAME=infra-1" in args
    assert not any(a.startswith("DOCKER_HOST") for a in args)
    assert args.count("HOME=/tmp") == 1
    assert not any(a == "HOME=/root" for a in args)
    # HOME=/tmp must be last so no later -e KEY can be mistaken for it.
    assert args[-2:] == ["-e", "HOME=/tmp"]


def test_redact_peer_cmd_hides_env_values() -> None:
    cmd = [
        "docker",
        "run",
        "--rm",
        "-e",
        "CONTROL_REPO_URL=git@example.com:org/secret.git",
        "-e",
        "HOME=/tmp",
        "image",
        "-c",
        "sh -c 'echo hi'",
    ]

    rendered = steward._redact_peer_cmd(cmd)

    assert "secret.git" not in rendered
    assert "-e CONTROL_REPO_URL " in rendered
    assert "-e HOME " in rendered
    assert "docker run --rm" in rendered


def test_redact_peer_cmd_preserves_non_env_tokens() -> None:
    cmd = ["docker", "run", "--rm", "-v", "/host:/host", "image"]

    rendered = steward._redact_peer_cmd(cmd)

    assert rendered == "docker run --rm -v /host:/host image"


def test_build_compose_up_cmd_without_env_file() -> None:
    app = _demo_app(pull_policy="missing")

    cmd = steward._build_compose_up_cmd(app, ["/host/git/docker-compose.yml"], None)

    assert cmd == [
        "docker",
        "compose",
        "--project-name",
        "demo",
        "-f",
        "/host/git/docker-compose.yml",
        "up",
        "-d",
        "--remove-orphans",
        "--pull",
        "missing",
    ]


def test_build_compose_up_cmd_with_env_file_and_override() -> None:
    app = _demo_app()

    cmd = steward._build_compose_up_cmd(
        app,
        ["/host/git/docker-compose.yml", "/host/git/docker-compose.override.yml"],
        "/host/git/.env",
    )

    assert cmd == [
        "docker",
        "compose",
        "--project-name",
        "demo",
        "-f",
        "/host/git/docker-compose.yml",
        "-f",
        "/host/git/docker-compose.override.yml",
        "--env-file",
        "/host/git/.env",
        "up",
        "-d",
        "--remove-orphans",
        "--pull",
        "always",
    ]


def test_run_peer_compose_returns_none_without_helper_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steward, "_get_helper_image", lambda: None)

    result = steward._run_peer_compose(
        _demo_app(), ["docker", "compose", "up"], [], detach=False, delay=0
    )

    assert result is None


def test_run_peer_compose_detached_shape_with_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward, "_peer_env_args", lambda: ["-e", "HOME=/tmp"])

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="abc123", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward._run_peer_compose(
        _demo_app(),
        [
            "docker",
            "compose",
            "--project-name",
            "steward",
            "-f",
            "/host/git/docker-compose.yml",
            "up",
            "-d",
            "--remove-orphans",
            "--pull",
            "always",
        ],
        ["/host/git:/host/git"],
        detach=True,
        delay=5,
    )

    assert result is not None
    assert result.stdout == "abc123"
    cmd = seen["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "-d" in cmd
    assert cmd[cmd.index("-d") + 1] == "--entrypoint"
    assert cmd[-1].startswith("sleep 5 && timeout 300 ")
    assert cmd[-3] == "ghcr.io/test/steward:latest"
    assert cmd[-2] == "-c"
    assert seen["kwargs"]["timeout"] == 30


def test_run_peer_compose_foreground_shape_without_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward, "_peer_env_args", lambda: ["-e", "HOME=/tmp"])

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward._run_peer_compose(
        _demo_app(),
        ["docker", "compose", "up"],
        ["/host/git:/host/git", "/host/git/stacks/app:/host/git/stacks/app"],
        detach=False,
        delay=0,
    )

    assert result is not None
    cmd = seen["cmd"]
    assert "-d" not in cmd
    assert cmd[:3] == ["docker", "run", "--rm"]
    v_indices = [i for i, v in enumerate(cmd) if v == "-v"]
    # first -v is always the docker socket
    assert cmd[v_indices[0] + 1] == "/var/run/docker.sock:/var/run/docker.sock"
    assert cmd[v_indices[1] + 1] == "/host/git:/host/git"
    assert cmd[v_indices[2] + 1] == "/host/git/stacks/app:/host/git/stacks/app"
    assert cmd[-1] == "timeout 300 docker compose up"
    assert seen["kwargs"]["timeout"] == 310


def test_run_peer_compose_does_not_catch_timeout_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    def _raise(*_args, **_kwargs):
        raise steward.subprocess.TimeoutExpired(cmd="docker", timeout=310)

    monkeypatch.setattr(steward.subprocess, "run", _raise)

    with pytest.raises(steward.subprocess.TimeoutExpired):
        steward._run_peer_compose(
            _demo_app(), ["docker", "compose", "up"], [], detach=False, delay=0
        )


def test_inc_creates_nested_counter() -> None:
    state = {}

    steward._inc(state, "apps", "demo", "reconcile_total", "success")
    steward._inc(state, "apps", "demo", "reconcile_total", "success", by=2)

    assert state["apps"]["demo"]["reconcile_total"]["success"] == 3


def test_spawn_compose_helper_falls_back_when_helper_image_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    app = _demo_app(name="steward")

    monkeypatch.setattr(steward, "_get_helper_image", lambda: None)
    monkeypatch.setattr(steward, "_resolve_host_path", lambda path: str(path))
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.spawn_compose_helper(app, tmp_path)

    assert result is True
    assert seen["cmd"][0:3] == ["docker", "compose", "--project-name"]
    assert "-d" in seen["cmd"]
    assert "run" not in seen["cmd"]
    assert seen["kwargs"]["timeout"] == 300


@pytest.mark.parametrize("reason", ["override", "env_file", "project_env"])
def test_spawn_compose_helper_validates_strict_paths_before_helper_image(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    app = _demo_app(name="steward")
    monkeypatch.setattr(
        steward,
        "_resolve_compose_host_paths",
        lambda *_args: (None, reason),
    )

    def _unexpected_image_lookup():
        raise AssertionError("helper image lookup must follow strict path validation")

    monkeypatch.setattr(steward, "_get_helper_image", _unexpected_image_lookup)

    assert steward.spawn_compose_helper(app, Path("/git/stacks/steward")) is False


def test_spawn_compose_helper_falls_back_when_host_path_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    app = _demo_app(name="steward")

    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward, "_resolve_compose_host_paths", lambda *_args: (None, "host_root"))
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.spawn_compose_helper(app, tmp_path)

    assert result is True
    assert seen["cmd"][0:3] == ["docker", "compose", "--project-name"]
    assert "run" not in seen["cmd"]
    assert seen["kwargs"]["timeout"] == 300


@pytest.mark.parametrize("reason", ["workdir", "compose_file"])
def test_spawn_compose_helper_falls_back_directly_for_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    app = _demo_app(name="steward")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(
        steward,
        "_resolve_compose_host_paths",
        lambda *_args: (None, reason),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.spawn_compose_helper(app, tmp_path)

    assert result is True
    assert seen["cmd"][0:3] == ["docker", "compose", "--project-name"]
    assert "run" not in seen["cmd"]


def test_spawn_compose_helper_hard_fails_on_unresolvable_override(
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
    monkeypatch.setattr(
        steward,
        "_resolve_compose_host_paths",
        lambda _app, _stack_path: (None, "override"),
    )

    fallback_called = {"value": False}

    def _fake_run_compose(_app, _stack_path):
        fallback_called["value"] = True
        return True

    monkeypatch.setattr(steward, "run_compose", _fake_run_compose)

    result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is False
    assert fallback_called["value"] is False


def test_spawn_compose_helper_hard_fails_on_unresolvable_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = steward.AppManifest(
        version=2,
        name="steward",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file="/git/stacks/steward/.env",
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(
        steward,
        "_resolve_compose_host_paths",
        lambda _app, _stack_path: (None, "env_file"),
    )

    fallback_called = {"value": False}

    def _fake_run_compose(_app, _stack_path):
        fallback_called["value"] = True
        return True

    monkeypatch.setattr(steward, "run_compose", _fake_run_compose)

    result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is False
    assert fallback_called["value"] is False


def test_spawn_compose_helper_forwards_env_and_redacts_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    monkeypatch.setattr(
        steward.os,
        "environ",
        {"CONTROL_REPO_URL": "git@example.com:org/super-secret.git", "HOME": "/root"},
    )

    seen_helper_cmd: list[str] = []

    def _fake_run(cmd, **_kwargs):
        seen_helper_cmd[:] = cmd
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    with caplog.at_level("DEBUG"):
        result = steward.spawn_compose_helper(app, Path("/git/stacks/steward"))

    assert result is True
    assert "-e" in seen_helper_cmd
    assert "CONTROL_REPO_URL=git@example.com:org/super-secret.git" in seen_helper_cmd
    assert seen_helper_cmd.count("HOME=/tmp") == 1
    assert "HOME=/root" not in seen_helper_cmd

    for record in caplog.records:
        assert "super-secret.git" not in record.getMessage()


@pytest.mark.parametrize("error", ["timeout", "not_found", "generic"])
def test_spawn_compose_helper_does_not_log_peer_error_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    error: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    app = _demo_app(name="steward")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward, "_resolve_host_path", lambda path: str(path))
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward.os, "environ", {"SECRET_TOKEN": "top-secret", "HOME": "/root"})

    def _raise(_cmd, **kwargs):
        if error == "timeout":
            raise steward.subprocess.TimeoutExpired(cmd=_cmd, timeout=kwargs["timeout"])
        if error == "not_found":
            raise FileNotFoundError("docker")
        raise RuntimeError("SECRET_TOKEN=top-secret")

    monkeypatch.setattr(steward.subprocess, "run", _raise)

    with caplog.at_level("DEBUG"):
        result = steward.spawn_compose_helper(app, tmp_path)

    assert result is False
    assert "top-secret" not in caplog.text
    if error == "timeout":
        assert "Self-update: helper launch timed out" in caplog.text
    elif error == "not_found":
        assert "Self-update: Docker not found" in caplog.text
    else:
        assert "Self-update: error launching helper (RuntimeError)" in caplog.text
        assert "SECRET_TOKEN=top-secret" not in caplog.text


def test_run_compose_uses_explicit_project_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

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
    _pin_direct_compose(monkeypatch)

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


def test_run_compose_includes_override_file_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

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
    f_indices = [i for i, v in enumerate(seen_cmd) if v == "-f"]
    assert len(f_indices) == 2
    assert seen_cmd[f_indices[0] + 1] == str(compose_file)
    assert seen_cmd[f_indices[1] + 1] == str(override_file)


def test_run_compose_omits_override_file_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

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
    f_indices = [i for i, v in enumerate(seen_cmd) if v == "-f"]
    assert len(f_indices) == 1
    assert seen_cmd[f_indices[0] + 1] == str(compose_file)


def test_run_compose_uses_peer_when_host_root_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    host_workdir = "/home/u/git/stacks/demo"
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): host_workdir,
                str(compose_file): f"{host_workdir}/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")
    monkeypatch.setattr(steward.os, "environ", {"HOME": "/root", "STACK_VAR": "value"})

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    cmd = seen["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert cmd[3:6] == ["--entrypoint", "sh", "-v"]
    assert "/home/u/git:/home/u/git" in cmd
    assert f"{host_workdir}:{host_workdir}" in cmd
    assert "-f /home/u/git/stacks/demo/docker-compose.yml" in cmd[-1]
    assert " /git" not in " ".join(cmd)
    assert "-d" not in cmd[: cmd.index("--entrypoint")]
    assert "timeout 300" in cmd[-1]
    assert seen["kwargs"]["timeout"] == 310


def test_run_compose_uses_peer_when_nested_workdir_mount_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/git",
                str(tmp_path): "/srv/demo",
                str(compose_file): "/srv/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    assert seen["cmd"][:3] == ["docker", "run", "--rm"]
    assert "/srv/demo:/srv/demo" in seen["cmd"]
    assert "-f /srv/demo/docker-compose.yml" in seen["cmd"][-1]


def test_run_compose_uses_direct_when_root_and_workdir_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    assert seen["cmd"][:3] == ["docker", "compose", "--project-name"]
    assert seen["kwargs"]["timeout"] == 300


def test_run_compose_fails_when_nested_workdir_mount_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path({"/git": "/git"}),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    def _unexpected_direct_run(*_args, **_kwargs):
        raise AssertionError("direct compose must not run")

    monkeypatch.setattr(steward.subprocess, "run", _unexpected_direct_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False


def test_run_compose_uses_direct_compatibility_path_when_host_root_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "_resolve_host_path", lambda _path: None)

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    assert seen["cmd"][0:3] == ["docker", "compose", "--project-name"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert seen["kwargs"]["timeout"] == 300


def test_run_compose_peer_helper_missing_returns_false_without_direct_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: None)

    def _unexpected_direct_run(*_args, **_kwargs):
        raise AssertionError("direct compose must not run")

    monkeypatch.setattr(steward.subprocess, "run", _unexpected_direct_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False


def test_run_compose_peer_quotes_spaced_paths_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stack_path = tmp_path / "stack space"
    stack_path.mkdir()
    compose_file = stack_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    env_file = stack_path / "app env"
    env_file.write_text("STACK_VAR=value\n")
    host_workdir = "/home/u/git/stack space"
    host_compose = f"{host_workdir}/docker-compose.yml"
    host_env = f"{host_workdir}/app env"
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(stack_path): host_workdir,
                str(compose_file): host_compose,
                str(env_file): host_env,
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(env_file=str(env_file)), stack_path)

    assert result is True
    script = seen["cmd"][-1]
    assert f"-f '{host_compose}'" in script
    assert f"--env-file '{host_env}'" in script
    assert script.count(f"'{host_compose}'") == 1
    assert script.count(f"'{host_env}'") == 1
    assert seen["kwargs"]["timeout"] == 310


def test_run_compose_peer_binds_separate_env_file_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    env_file = tmp_path / "app.env"
    env_file.write_text("STACK_VAR=value\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                str(env_file): "/opt/secrets/app.env",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(env_file=str(env_file)), tmp_path)

    assert result is True
    assert "-v" in seen["cmd"]
    assert "/opt/secrets/app.env:/opt/secrets/app.env:ro" in seen["cmd"]
    assert "--env-file /opt/secrets/app.env" in seen["cmd"][-1]


def test_run_compose_peer_propagates_resolvable_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                str(override_file): "/opt/overrides/docker-compose.override.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    script = seen["cmd"][-1]
    assert script.index("-f /home/u/git/stacks/demo/docker-compose.yml") < script.index(
        "-f /opt/overrides/docker-compose.override.yml"
    )
    assert (
        seen["cmd"].count(
            "/opt/overrides/docker-compose.override.yml:/opt/overrides/docker-compose.override.yml:ro"
        )
        == 1
    )
    assert (
        seen["cmd"].count(
            "/home/u/git/stacks/demo/docker-compose.yml:/home/u/git/stacks/demo/docker-compose.yml:ro"
        )
        == 0
    )


def test_run_compose_peer_preserves_implicit_project_env_without_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    project_env = tmp_path / ".env"
    project_env.write_text("IMAGE_TAG=stable\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                str(project_env): "/opt/secrets/demo.env",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(steward, "_get_helper_image", lambda: "ghcr.io/test/steward:latest")

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(steward.subprocess, "run", _fake_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is True
    assert "/opt/secrets/demo.env:/home/u/git/stacks/demo/.env:ro" in seen["cmd"]
    assert "--env-file" not in seen["cmd"][-1]


@pytest.mark.parametrize("error", ["timeout", "not_found"])
def test_run_compose_direct_handles_subprocess_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    error: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

    def _raise(*_args, **kwargs):
        if error == "timeout":
            raise steward.subprocess.TimeoutExpired(cmd="docker", timeout=310)
        raise FileNotFoundError("docker")

    monkeypatch.setattr(steward.subprocess, "run", _raise)

    with caplog.at_level("ERROR"):
        result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False
    if error == "timeout":
        assert "docker compose timed out for app 'demo'" in caplog.text
    else:
        assert "docker compose not found" in caplog.text


@pytest.mark.parametrize("error", ["timeout", "not_found"])
def test_run_compose_peer_handles_subprocess_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    error: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    def _raise(*_args, **_kwargs):
        if error == "timeout":
            raise steward.subprocess.TimeoutExpired(cmd="docker", timeout=310)
        raise FileNotFoundError("docker")

    monkeypatch.setattr(steward, "_run_peer_compose", _raise)

    with caplog.at_level("ERROR"):
        result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False
    if error == "timeout":
        assert "docker compose timed out for app 'demo'" in caplog.text
    else:
        assert "docker compose not found" in caplog.text


def test_resolve_compose_host_paths_preserves_named_volume_and_workdir_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "git"
    workdir = root / "stacks" / "demo"
    workdir.mkdir(parents=True)
    compose_file = workdir / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", root)
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                str(root): "/var/lib/docker/volumes/x/_data",
                str(workdir): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )

    paths, reason = steward._resolve_compose_host_paths(_demo_app(), workdir)

    assert reason == ""
    assert paths is not None
    assert "/var/lib/docker/volumes/x/_data:/var/lib/docker/volumes/x/_data" in paths.bind_specs
    assert "/home/u/git/stacks/demo:/home/u/git/stacks/demo" in paths.bind_specs


def test_run_compose_peer_rejects_unresolvable_override_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")

    monkeypatch.setattr(
        steward,
        "_run_peer_compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("peer compose must not run")
        ),
    )

    def _unexpected_direct_run(*_args, **_kwargs):
        raise AssertionError("direct compose must not run")

    monkeypatch.setattr(steward.subprocess, "run", _unexpected_direct_run)

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False


def test_run_compose_peer_rejects_missing_env_file_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    missing_env = tmp_path / "missing.env"
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(
        steward,
        "_run_peer_compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("peer compose must not run")
        ),
    )

    result = steward.run_compose(_demo_app(env_file=str(missing_env)), tmp_path)

    assert result is False


def test_run_compose_peer_rejects_unresolvable_env_file_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    env_file = tmp_path / "app.env"
    env_file.write_text("STACK_VAR=value\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(
        steward,
        "_run_peer_compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("peer compose must not run")
        ),
    )

    result = steward.run_compose(_demo_app(env_file=str(env_file)), tmp_path)

    assert result is False


def test_run_compose_peer_rejects_unresolvable_project_env_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    project_env = tmp_path / ".env"
    project_env.write_text("IMAGE_TAG=stable\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
                # project_env deliberately absent
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(
        steward,
        "_run_peer_compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("peer compose must not run")
        ),
    )

    result = steward.run_compose(_demo_app(), tmp_path)

    assert result is False


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_run_compose_peer_returns_status_and_logs_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    returncode: int,
    expected: bool,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(steward, "GITOPS_ROOT", Path("/git"))
    monkeypatch.setattr(
        steward,
        "_resolve_host_path",
        _stub_resolve_host_path(
            {
                "/git": "/home/u/git",
                str(tmp_path): "/home/u/git/stacks/demo",
                str(compose_file): "/home/u/git/stacks/demo/docker-compose.yml",
            }
        ),
    )
    monkeypatch.setattr(steward, "host_path", lambda _path: "<host path>")
    monkeypatch.setattr(
        steward,
        "_run_peer_compose",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout="peer stdout\n",
            stderr="peer stderr\n",
        ),
    )

    with caplog.at_level("INFO"):
        result = steward.run_compose(_demo_app(), tmp_path)

    assert result is expected
    assert "Reconciling app 'demo' via peer helper: docker compose" in caplog.text
    assert "[compose/demo] peer stdout" in caplog.text
    assert "[compose/demo] peer stderr" in caplog.text
    if not expected:
        assert "docker compose exited with code 1 for app 'demo'" in caplog.text


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_run_compose_direct_returns_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    expected: bool,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    _pin_direct_compose(monkeypatch)

    monkeypatch.setattr(
        steward.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout="",
            stderr="",
        ),
    )

    assert steward.run_compose(_demo_app(), tmp_path) is expected


def test_load_compose_services_status_includes_override_file_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: nginx\n")
    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text("services: {}\n")

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

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = '[{"Service":"web","State":"running"}]\n'
        return result

    monkeypatch.setattr(steward.subprocess, "run", fake_run)

    rows = steward._load_compose_services_status(app, tmp_path)

    assert rows == [{"Service": "web", "State": "running"}]
    f_indices = [i for i, v in enumerate(calls[0]) if v == "-f"]
    assert len(f_indices) == 2
    assert calls[0][f_indices[1] + 1] == str(override_file)


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


def test_reconcile_app_sets_unknown_status_on_fetch_failure(
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
    )

    state: dict = {}
    repo = MagicMock()

    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: repo)
    monkeypatch.setattr(steward, "fetch_ref", lambda _repo, _ref: False)

    result = steward.reconcile_app(app, state)

    assert result is False
    assert state["apps"]["demo"]["sync_status"] == "Unknown"
    assert state["apps"]["demo"]["health_status"] == "Unknown"


def test_sqlite_state_roundtrip_includes_sync_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_evaluate_health_status_degraded_manual_no_auto_apply(
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


def test_evaluate_health_status_degraded_auto_attempts_apply(
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


def test_load_expected_services_passes_compose_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("VAR=1\n")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: nginx\n")

    # compose_env_file is normalised to env_file by the manifest parser
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=str(env_file),
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = "web\n"
        return result

    monkeypatch.setattr(steward.subprocess, "run", fake_run)

    services = steward._load_expected_services(app, tmp_path)

    assert services == {"web"}
    assert "--env-file" in calls[0]
    assert str(env_file) in calls[0]
    # --env-file must come before the subcommand
    assert calls[0].index("--env-file") < calls[0].index("config")


def test_load_compose_services_status_passes_compose_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("VAR=1\n")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: nginx\n")

    # compose_env_file is normalised to env_file by the manifest parser
    app = steward.AppManifest(
        version=2,
        name="demo",
        repo="git@example.com:org/repo.git",
        ref=steward.AppRef(branch="main"),
        path=".",
        compose_file="docker-compose.yml",
        env_file=str(env_file),
        enabled=True,
        source_file=Path("/tmp/app.yml"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = '[{"Service":"web","State":"running"}]\n'
        return result

    monkeypatch.setattr(steward.subprocess, "run", fake_run)

    rows = steward._load_compose_services_status(app, tmp_path)

    assert rows == [{"Service": "web", "State": "running"}]
    assert "--env-file" in calls[0]
    assert str(env_file) in calls[0]
    # --env-file must come before the subcommand
    assert calls[0].index("--env-file") < calls[0].index("ps")


def test_reconcile_app_synced_drift_manual_logs_skipped_operation(
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
    monkeypatch.setattr(
        steward,
        "_detect_live_drift",
        lambda _app, _stack: (True, "live_drift_detected[db:missing]"),
    )
    monkeypatch.setattr(steward, "_evaluate_health_status", lambda _app, _stack, _state: "Degraded")

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        steward, "_send_notification", lambda _app, event, payload: sent.append((event, payload))
    )

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
    monkeypatch.setattr(
        steward,
        "_detect_live_drift",
        lambda _app, _stack: (True, "live_drift_detected[db:missing]"),
    )
    healed_calls: list[str] = []
    monkeypatch.setattr(steward, "_is_self_update", lambda _app: True)
    monkeypatch.setattr(
        steward,
        "spawn_compose_helper",
        lambda _app, _stack: healed_calls.append("spawn") or True,
    )

    def _unexpected_run_compose(_app, _stack):
        raise AssertionError("self-update must not use run_compose")

    monkeypatch.setattr(steward, "run_compose", _unexpected_run_compose)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["sync_status"] == "Synced"
    assert state["apps"]["demo"]["health_status"] == "Progressing"
    assert state["apps"]["demo"]["sync_total"]["success"] == 1
    assert state["_operations"][0]["trigger"] == "self_heal"
    assert healed_calls == ["spawn"]


def test_reconcile_app_synced_expected_services_unavailable_reports_unknown(
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


def test_reconcile_app_synced_live_state_unavailable_reports_unknown(
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
        steward, "_detect_live_drift", lambda _app, _stack: (False, "live_state_unavailable")
    )

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
    monkeypatch.setattr(
        steward,
        "sync_app",
        lambda _app, _repo, _path: steward.SyncResult(success=False, message="compose_failed"),
    )

    sent: list[str] = []
    monkeypatch.setattr(
        steward, "_send_notification", lambda _app, event, payload: sent.append(event)
    )

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
    # entrypoint must be overridden so entrypoint.sh / crond is bypassed
    assert "--entrypoint" in seen_helper_cmd
    assert seen_helper_cmd[seen_helper_cmd.index("--entrypoint") + 1] == "sh"
    helper_shell = seen_helper_cmd[-1]
    assert "--project-name steward" in helper_shell
    assert "timeout 300" in helper_shell


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


def test_sync_app_self_update_uses_spawn_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _demo_app(name=steward.AGENT_CONTAINER_NAME)
    calls: list[str] = []

    monkeypatch.setattr(steward, "apply_ref", lambda _repo, _ref: True)
    monkeypatch.setattr(
        steward, "spawn_compose_helper", lambda _app, _stack: calls.append("spawn") or True
    )

    def _unexpected_run_compose(_app, _stack):
        raise AssertionError("self-update must not use run_compose")

    monkeypatch.setattr(steward, "run_compose", _unexpected_run_compose)

    result = steward.sync_app(app, MagicMock(), Path("/tmp/demo"))

    assert result.success is True
    assert calls == ["spawn"]


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


def test_prune_apps_removes_stale_app_rows(tmp_path: Path) -> None:
    """An app no longer present in the manifests (e.g. renamed) is deleted from the DB."""
    db_file = tmp_path / "steward.db"
    node = "test-node"

    state = {
        "node": node,
        "reconcile": {},
        "apps": {
            "hermes": {"sync_status": "OutOfSync", "health_status": "Healthy"},
            "hermes-compose": {"sync_status": "Synced", "health_status": "Healthy"},
        },
    }
    state_store.save_state(db_file, node, state)

    removed = state_store.prune_apps(db_file, node, keep_apps={"hermes-compose"})

    assert removed == ["hermes"]
    loaded = state_store.load_state(db_file, node)
    assert "hermes" not in loaded["apps"]
    assert "hermes-compose" in loaded["apps"]


def test_prune_apps_refuses_empty_keep_set(tmp_path: Path) -> None:
    """An empty keep-set is a no-op, guarding against wiping state on a bad manifest read."""
    db_file = tmp_path / "steward.db"
    node = "test-node"

    state_store.save_state(
        db_file, node, {"node": node, "reconcile": {}, "apps": {"demo": {"sync_status": "Synced"}}}
    )

    removed = state_store.prune_apps(db_file, node, keep_apps=set())

    assert removed == []
    loaded = state_store.load_state(db_file, node)
    assert "demo" in loaded["apps"]


def test_reconcile_prunes_renamed_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconcile removes stale state for an app renamed/removed from the manifests."""
    renamed_app = steward.AppManifest(
        version=2,
        name="hermes-compose",
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

        def close(self) -> None:
            pass

    monkeypatch.setattr(steward, "DB_FILE", tmp_path / "steward.db")
    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([renamed_app], []))

    state_store.save_state(
        steward.DB_FILE,
        steward.GITOPS_NODE_NAME,
        {
            "node": steward.GITOPS_NODE_NAME,
            "reconcile": {},
            "apps": {"hermes": {"sync_status": "OutOfSync", "health_status": "Healthy"}},
        },
    )

    result = steward.reconcile()

    assert result == 0
    loaded = state_store.load_state(steward.DB_FILE, steward.GITOPS_NODE_NAME)
    assert "hermes" not in loaded["apps"]
    assert "hermes-compose" in loaded["apps"]


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

        def close(self) -> None:
            pass

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})

    saved: dict = {}

    def _fake_save(state: dict) -> None:
        saved.update(state)

    monkeypatch.setattr(steward, "_save_metrics_state", _fake_save)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: _FakeRepo())
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([disabled_app], []))

    result = steward.reconcile()

    assert result == 0
    assert saved["apps"]["demo"]["sync_status"] == "Disabled"


def test_reconcile_never_commits_or_pushes_to_control_repo(
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

    control_repo = MagicMock()
    control_repo.working_dir = "/tmp/control"

    monkeypatch.setattr(steward, "CONTROL_REPO_URL", "git@example.com:org/control.git")
    monkeypatch.setattr(steward, "_load_metrics_state", lambda: {"node": steward.GITOPS_NODE_NAME})
    monkeypatch.setattr(steward, "_save_metrics_state", lambda _state: None)
    monkeypatch.setattr(steward, "ensure_repo", lambda **_kwargs: control_repo)
    monkeypatch.setattr(steward, "sync_repo", lambda _repo, _ref: False)
    monkeypatch.setattr(steward, "load_node_manifests", lambda _repo: ([disabled_app], []))

    result = steward.reconcile()

    assert result == 0
    # steward must never write to the control repo (GitOps: git holds desired state only).
    assert control_repo.index.add.call_count == 0
    assert control_repo.index.commit.call_count == 0
    assert control_repo.git.push.call_count == 0


def _commit_file(repo: Repo, name: str, content: str, message: str) -> str:
    file_path = Path(repo.working_dir) / name
    file_path.write_text(content)
    repo.index.add([name])
    return repo.index.commit(message).hexsha


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Repo, Repo]:
    upstream_path = tmp_path / "upstream"
    upstream = Repo.init(upstream_path, initial_branch="main")
    with upstream.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    _commit_file(upstream, "file.txt", "A\n", "commit A")

    local_path = tmp_path / "local"
    local = Repo.clone_from(str(upstream_path), str(local_path))
    with local.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    return upstream, local


def test_apply_ref_fast_forwards_when_branch_is_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, local = _make_upstream_and_clone(tmp_path)
    new_sha = _commit_file(upstream, "file.txt", "B\n", "commit B")

    warnings: list[str] = []
    monkeypatch.setattr(steward.log, "warning", lambda msg, *args: warnings.append(msg % args))

    result = steward.apply_ref(local, steward.AppRef(branch="main"))

    assert result is True
    # Fast-forwarded to the remote tip without a hard reset (no divergence warning).
    assert local.head.commit.hexsha == new_sha
    assert warnings == []


def test_apply_ref_hard_resets_divergent_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, local = _make_upstream_and_clone(tmp_path)
    upstream_sha = _commit_file(upstream, "file.txt", "B\n", "commit B")
    local_only_sha = _commit_file(local, "file.txt", "C\n", "local commit C")

    warnings: list[str] = []
    monkeypatch.setattr(steward.log, "warning", lambda msg, *args: warnings.append(msg % args))

    result = steward.apply_ref(local, steward.AppRef(branch="main"))

    assert result is True
    # Working copy is forced to the remote tip; the local-only commit is discarded.
    assert local.head.commit.hexsha == upstream_sha
    assert local_only_sha not in {c.hexsha for c in local.iter_commits()}
    assert any("diverged" in m for m in warnings)


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
    monkeypatch.setattr(
        steward,
        "_detect_live_drift",
        lambda _app, _stack: (True, "live_drift_detected[db:missing]"),
    )
    monkeypatch.setattr(steward, "run_compose", lambda _app, _stack: True)

    result = steward.reconcile_app(app, state)

    assert result is True
    assert state["apps"]["demo"]["ooband_heal_total"] == 1


def test_operation_retention_prunes_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

        def close(self) -> None:
            pass

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

    result = steward.reconcile()

    assert result == 1
    assert saved["apps"]["steward"]["sync_status"] == steward.SyncStatus.UNKNOWN.value
    assert saved["apps"]["steward"]["health_status"] == steward.HEALTH_STATUS_UNKNOWN
    assert saved["apps"]["steward"]["reconcile_total"]["failed"] >= 1
    assert saved["reconcile"]["total"]["partial_failure"] == 1
    assert saved["reconcile"]["manifest_parse_errors"] == 1


def test_reconcile_parse_error_run_result_is_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with only parse errors records partial_failure, not success."""

    class _FakeRepo:
        working_dir = "/tmp/control"

        def close(self) -> None:
            pass

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

    result = steward.reconcile()

    assert result == 1
    assert "success" not in saved.get("reconcile", {}).get("total", {})
    assert saved["reconcile"]["total"]["partial_failure"] == 1
    assert saved["reconcile"]["manifest_parse_errors"] == 2
