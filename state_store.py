from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reconcile_state (
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

CREATE TABLE IF NOT EXISTS app_state (
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

CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app TEXT NOT NULL,
  node TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  trigger TEXT NOT NULL,
  from_sha TEXT,
  to_sha TEXT NOT NULL,
  sync_status TEXT NOT NULL,
  health_status TEXT,
  duration_s REAL,
  message TEXT
);

CREATE TABLE IF NOT EXISTS app_manifest (
  app TEXT NOT NULL,
  node TEXT NOT NULL,
  repo TEXT NOT NULL,
  ref_type TEXT NOT NULL,
  ref_value TEXT NOT NULL,
  sync_policy TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (app, node)
);
"""


def _connect(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=3000;")
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def load_state(db_file: Path, node_name: str, require_data: bool = False) -> dict:
    conn = _connect(db_file)
    _init(conn)

    state: dict = {"node": node_name, "reconcile": {}, "apps": {}}

    rec = conn.execute(
        "SELECT * FROM reconcile_state WHERE node = ?",
        (node_name,),
    ).fetchone()
    if rec:
        rec_state = state["reconcile"]
        if rec["last_timestamp"] is not None:
            rec_state["last_timestamp"] = rec["last_timestamp"]
        if rec["last_duration_seconds"] is not None:
            rec_state["last_duration_seconds"] = rec["last_duration_seconds"]

        rec_state["total"] = {
            "success": rec["total_success"],
            "partial_failure": rec["total_partial_failure"],
            "fatal": rec["total_fatal"],
        }
        rec_state["control_repo_sync_total"] = {
            "up_to_date": rec["control_repo_sync_up_to_date"],
            "updated": rec["control_repo_sync_updated"],
            "failed": rec["control_repo_sync_failed"],
        }
        rec_state["manifest_parse_errors"] = rec["manifest_parse_errors"]

    app_rows = conn.execute(
        "SELECT * FROM app_state WHERE node = ? ORDER BY app",
        (node_name,),
    ).fetchall()
    for row in app_rows:
        app_state = {
            "repo": row["repo"] or "",
            "ref": row["ref"] or "",
            "ref_type": row["ref_type"] or "",
            "enabled": bool(row["enabled"]),
            "reconcile_total": {
                "success": row["reconcile_success"],
                "failed": row["reconcile_failed"],
                "skipped": row["reconcile_skipped"],
            },
            "sync_total": {
                "success": row["sync_success"],
                "failed": row["sync_failed"],
            },
        }
        if row["last_reconcile_timestamp"] is not None:
            app_state["last_reconcile_timestamp"] = row["last_reconcile_timestamp"]
        if row["last_sync_timestamp"] is not None:
            app_state["last_sync_timestamp"] = row["last_sync_timestamp"]

        state["apps"][row["app"]] = app_state

    conn.close()

    if require_data and not rec and not app_rows:
        raise FileNotFoundError(db_file)

    return state


def save_state(db_file: Path, node_name: str, state: dict) -> None:
    conn = _connect(db_file)
    _init(conn)

    node = state.get("node", node_name)
    rec = state.get("reconcile", {})
    total = rec.get("total", {})
    ctrl = rec.get("control_repo_sync_total", {})

    conn.execute(
        """
        INSERT INTO reconcile_state (
          node, last_timestamp, last_duration_seconds,
          total_success, total_partial_failure, total_fatal,
          control_repo_sync_up_to_date, control_repo_sync_updated, control_repo_sync_failed,
          manifest_parse_errors
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node) DO UPDATE SET
          last_timestamp = excluded.last_timestamp,
          last_duration_seconds = excluded.last_duration_seconds,
          total_success = excluded.total_success,
          total_partial_failure = excluded.total_partial_failure,
          total_fatal = excluded.total_fatal,
          control_repo_sync_up_to_date = excluded.control_repo_sync_up_to_date,
          control_repo_sync_updated = excluded.control_repo_sync_updated,
          control_repo_sync_failed = excluded.control_repo_sync_failed,
          manifest_parse_errors = excluded.manifest_parse_errors
        """,
        (
            node,
            rec.get("last_timestamp"),
            rec.get("last_duration_seconds"),
            total.get("success", 0),
            total.get("partial_failure", 0),
            total.get("fatal", 0),
            ctrl.get("up_to_date", 0),
            ctrl.get("updated", 0),
            ctrl.get("failed", 0),
            rec.get("manifest_parse_errors", 0),
        ),
    )

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for app_name, app in state.get("apps", {}).items():
        app_rec = app.get("reconcile_total", {})
        app_sync = app.get("sync_total", {})
        conn.execute(
            """
            INSERT INTO app_state (
              app, node, repo, ref, ref_type, enabled,
              last_reconcile_timestamp, last_sync_timestamp,
              reconcile_success, reconcile_failed, reconcile_skipped,
              sync_success, sync_failed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app, node) DO UPDATE SET
              repo = excluded.repo,
              ref = excluded.ref,
              ref_type = excluded.ref_type,
              enabled = excluded.enabled,
              last_reconcile_timestamp = excluded.last_reconcile_timestamp,
              last_sync_timestamp = excluded.last_sync_timestamp,
              reconcile_success = excluded.reconcile_success,
              reconcile_failed = excluded.reconcile_failed,
              reconcile_skipped = excluded.reconcile_skipped,
              sync_success = excluded.sync_success,
              sync_failed = excluded.sync_failed
            """,
            (
                app_name,
                node,
                app.get("repo", ""),
                app.get("ref", ""),
                app.get("ref_type", ""),
                1 if app.get("enabled", True) else 0,
                app.get("last_reconcile_timestamp"),
                app.get("last_sync_timestamp"),
                app_rec.get("success", 0),
                app_rec.get("failed", 0),
                app_rec.get("skipped", 0),
                app_sync.get("success", 0),
                app_sync.get("failed", 0),
            ),
        )

        conn.execute(
            """
            INSERT INTO app_manifest (
              app, node, repo, ref_type, ref_value, sync_policy, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app, node) DO UPDATE SET
              repo = excluded.repo,
              ref_type = excluded.ref_type,
              ref_value = excluded.ref_value,
              sync_policy = excluded.sync_policy,
              enabled = excluded.enabled,
              updated_at = excluded.updated_at
            """,
            (
                app_name,
                node,
                app.get("repo", ""),
                app.get("ref_type", ""),
                app.get("ref", ""),
                "auto",
                1 if app.get("enabled", True) else 0,
                now_iso,
            ),
        )

    for op in state.get("_operations", []):
        conn.execute(
            """
            INSERT INTO operations (
              app, node, started_at, completed_at, trigger,
              from_sha, to_sha, sync_status, health_status, duration_s, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                op["app"],
                op.get("node", node),
                op["started_at"],
                op.get("completed_at"),
                op.get("trigger", "git_change"),
                op.get("from_sha"),
                op.get("to_sha", ""),
                op.get("sync_status", "Failed"),
                op.get("health_status"),
                op.get("duration_s"),
                op.get("message"),
            ),
        )

    conn.commit()
    conn.close()
