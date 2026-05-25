import sqlite3
from pathlib import Path

import pytest

import metrics_server


def _prepare_db(path: Path, node: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE reconcile_state (
          node TEXT PRIMARY KEY,
          last_timestamp REAL,
          last_duration_seconds REAL,
          total_success INTEGER NOT NULL DEFAULT 0,
          total_partial_failure INTEGER NOT NULL DEFAULT 0,
          total_fatal INTEGER NOT NULL DEFAULT 0,
          control_repo_sync_up_to_date INTEGER NOT NULL DEFAULT 0,
          control_repo_sync_updated INTEGER NOT NULL DEFAULT 0,
          control_repo_sync_failed INTEGER NOT NULL DEFAULT 0,
          manifest_parse_errors INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE app_state (
          app TEXT NOT NULL,
          node TEXT NOT NULL,
          repo TEXT,
          ref TEXT,
          ref_type TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          last_reconcile_timestamp REAL,
          last_sync_timestamp REAL,
          reconcile_success INTEGER NOT NULL DEFAULT 0,
          reconcile_failed INTEGER NOT NULL DEFAULT 0,
          reconcile_skipped INTEGER NOT NULL DEFAULT 0,
          sync_success INTEGER NOT NULL DEFAULT 0,
          sync_failed INTEGER NOT NULL DEFAULT 0,
          sync_status TEXT,
          health_status TEXT,
          deployed_sha TEXT,
          remote_sha TEXT,
          last_checked TEXT,
          last_synced TEXT,
          PRIMARY KEY (app, node)
        );

        CREATE TABLE operations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          app TEXT NOT NULL,
          node TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          trigger TEXT,
          from_sha TEXT,
          to_sha TEXT,
          sync_status TEXT,
          health_status TEXT,
          duration_s REAL,
          message TEXT
        );
        """
    )

    conn.execute(
        """
        INSERT INTO reconcile_state (
          node, last_timestamp, last_duration_seconds,
          total_success, total_partial_failure, total_fatal,
          control_repo_sync_up_to_date, control_repo_sync_updated, control_repo_sync_failed,
          manifest_parse_errors
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (node, 10.0, 1.5, 2, 1, 0, 3, 4, 1, 5),
    )

    conn.execute(
        """
        INSERT INTO app_state (
          app, node, repo, ref, ref_type, enabled,
          last_reconcile_timestamp, last_sync_timestamp,
          reconcile_success, reconcile_failed, reconcile_skipped,
          sync_success, sync_failed, sync_status, health_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "demo",
            node,
            "git@example.com:org/repo.git",
            "main",
            "branch",
            1,
            11.0,
            12.0,
            6,
            2,
            1,
            4,
            1,
            "OutOfSync",
            "Degraded",
        ),
    )

    conn.commit()
    conn.close()


def test_load_state_from_db_and_format_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "steward.db"
    node = "node-a"
    _prepare_db(db_path, node)

    monkeypatch.setattr(metrics_server, "DB_FILE", db_path)
    monkeypatch.setattr(metrics_server, "NODE_NAME", node)

    state = metrics_server._load_state_from_db()

    assert state["node"] == node
    assert state["reconcile"]["total"]["partial_failure"] == 1
    assert state["apps"]["demo"]["sync_total"]["success"] == 4

    body = metrics_server.format_metrics(state)
    assert "steward_reconcile_total" in body
    assert 'app="demo"' in body
    assert 'steward_app_sync_status{node="node-a",app="demo",status="OutOfSync"} 1' in body
    assert 'steward_app_sync_status{node="node-a",app="demo",status="Synced"} 0' in body
    assert 'steward_app_health_status{node="node-a",app="demo",status="Degraded"} 1' in body
    assert 'steward_app_health_status{node="node-a",app="demo",status="Healthy"} 0' in body
    assert 'steward_app_ooband_heal_total{node="node-a",app="demo"} 0' in body


def test_oob_heal_metric_uses_operations_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "steward.db"
    node = "node-a"
    _prepare_db(db_path, node)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO operations (
          app, node, started_at, completed_at, trigger, from_sha, to_sha,
          sync_status, health_status, duration_s, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "demo",
            node,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            "self_heal",
            "abc",
            "abc",
            "Synced",
            "Healthy",
            1.0,
            "live_drift_detected[db:missing]",
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(metrics_server, "DB_FILE", db_path)
    monkeypatch.setattr(metrics_server, "NODE_NAME", node)

    body = metrics_server.format_metrics(metrics_server._load_state_from_db())

    assert 'steward_app_ooband_heal_total{node="node-a",app="demo"} 1' in body


def test_load_state_from_db_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(metrics_server, "DB_FILE", missing)

    with pytest.raises(FileNotFoundError):
        metrics_server._load_state_from_db()
