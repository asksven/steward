#!/usr/bin/env python3
"""
Lightweight Prometheus metrics server for steward.
Reads the metrics state file written by steward.py and serves /metrics
in Prometheus text exposition format.
"""

import http.server
import json
import os
import sys
from pathlib import Path

STATE_FILE = Path(os.environ.get("GITOPS_ROOT", "/git")) / "metrics" / "state.json"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9101


def _labels(**kw) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in kw.items()) + "}"


def format_metrics(state: dict) -> str:
    lines: list[str] = []
    defined: set[str] = set()
    node = state.get("node", "unknown")

    def write(help_text: str, metric_type: str, name: str, value, **lbs):
        if name not in defined:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
            defined.add(name)
        lines.append(f"{name}{_labels(**lbs)} {value}")

    # --- node-level ---
    rec = state.get("reconcile", {})

    if "last_timestamp" in rec:
        write("Unix timestamp of last completed reconciliation run", "gauge",
              "steward_reconcile_last_timestamp_seconds",
              rec["last_timestamp"], node=node)

    if "last_duration_seconds" in rec:
        write("Duration of last reconciliation run in seconds", "gauge",
              "steward_reconcile_duration_seconds",
              rec["last_duration_seconds"], node=node)

    for result, count in rec.get("total", {}).items():
        write("Total reconciliation runs by result", "counter",
              "steward_reconcile_total", count, node=node, result=result)

    for result, count in rec.get("control_repo_sync_total", {}).items():
        write("Total control repo sync attempts by result", "counter",
              "steward_control_repo_sync_total", count, node=node, result=result)

    if "manifest_parse_errors" in rec:
        write("Total manifest parse errors encountered", "counter",
              "steward_manifest_parse_errors_total",
              rec["manifest_parse_errors"], node=node)

    # --- app-level ---
    for app_name, app in state.get("apps", {}).items():
        base = dict(node=node, app=app_name)

        write("Application information", "gauge", "steward_app_info", 1,
              node=node, app=app_name,
              repo=app.get("repo", ""),
              ref=app.get("ref", ""),
              ref_type=app.get("ref_type", ""),
              enabled=str(app.get("enabled", True)).lower())

        if "last_reconcile_timestamp" in app:
            write("Unix timestamp of last reconcile attempt", "gauge",
                  "steward_app_last_reconcile_timestamp_seconds",
                  app["last_reconcile_timestamp"], **base)

        if "last_sync_timestamp" in app:
            write("Unix timestamp of last docker compose run", "gauge",
                  "steward_app_last_sync_timestamp_seconds",
                  app["last_sync_timestamp"], **base)

        for result, count in app.get("reconcile_total", {}).items():
            write("Total reconcile attempts per app by result", "counter",
                  "steward_app_reconcile_total", count, **base, result=result)

        for result, count in app.get("sync_total", {}).items():
            write("Total docker compose runs per app by result", "counter",
                  "steward_app_sync_total", count, **base, result=result)

    lines.append("")
    return "\n".join(lines)


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            try:
                state = json.loads(STATE_FILE.read_text())
                body = format_metrics(state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            except FileNotFoundError:
                body = b"# No metrics yet - steward has not completed a reconciliation run\n"
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
        else:
            body = b"steward metrics server - GET /metrics\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")

        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("", PORT), MetricsHandler)
    print(f"Metrics server listening on :{PORT}", flush=True)
    server.serve_forever()
