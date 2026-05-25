# Steward — Prometheus & Grafana Setup

## Prometheus scrape config

Add one target per node to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: steward
    scrape_interval: 30s
    static_configs:
      - targets:
          - media-1:9101
          - media-2:9101
        labels:
          environment: homelab
```

---

## Grafana dashboard

**Import via:** Dashboards → New → Import → paste the JSON below → select your Prometheus datasource when prompted.

```json
{
  "__inputs": [
    {
      "name": "DS_PROMETHEUS",
      "label": "Prometheus",
      "description": "",
      "type": "datasource",
      "pluginId": "prometheus",
      "pluginName": "Prometheus"
    }
  ],
  "__elements": {},
  "__requires": [
    {"type": "grafana",    "id": "grafana",    "name": "Grafana",     "version": "9.0.0"},
    {"type": "datasource", "id": "prometheus", "name": "Prometheus",  "version": "1.0.0"},
    {"type": "panel",      "id": "stat",       "name": "Stat",        "version": ""},
    {"type": "panel",      "id": "table",      "name": "Table",       "version": ""},
    {"type": "panel",      "id": "timeseries", "name": "Time series", "version": ""}
  ],
  "annotations": {
    "list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
              "enable": true, "hide": true, "iconColor": "rgba(0, 211, 255, 1)",
              "name": "Annotations & Alerts", "type": "dashboard"}]
  },
  "description": "Steward GitOps agent — reconciliation, sync, health, and drift status",
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 1,
  "id": null,
  "links": [],
  "refresh": "1m",
  "schemaVersion": 38,
  "tags": ["steward", "gitops"],
  "time": {"from": "now-3h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "title": "Steward GitOps",
  "uid": "steward-gitops",
  "version": 1,
  "templating": {
    "list": [
      {
        "current": {"selected": true, "text": "All", "value": "$__all"},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "definition": "label_values(steward_reconcile_last_timestamp_seconds, node)",
        "hide": 0,
        "includeAll": true,
        "multi": true,
        "name": "node",
        "options": [],
        "query": {
          "query": "label_values(steward_reconcile_last_timestamp_seconds, node)",
          "refId": "StandardVariableQuery"
        },
        "refresh": 2,
        "regex": "",
        "sort": 1,
        "type": "query"
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "title": "Node Last Seen",
      "description": "Seconds since last completed reconciliation. Red = node has not reported for > 5 min.",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 0, "y": 0},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "time() - steward_reconcile_last_timestamp_seconds{node=~\"$node\"}",
          "instant": true,
          "legendFormat": "{{node}}",
          "refId": "A"
        }
      ],
      "options": {
        "reduceOptions": {"values": false, "calcs": ["lastNotNull"], "fields": ""},
        "orientation": "auto", "textMode": "auto",
        "colorMode": "background", "graphMode": "none", "justifyMode": "auto"
      },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "displayName": "${__field.labels.node}",
          "color": {"mode": "thresholds"},
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green",  "value": null},
              {"color": "yellow", "value": 120},
              {"color": "red",    "value": 300}
            ]
          }
        },
        "overrides": []
      }
    },
    {
      "id": 2,
      "title": "Active Apps",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 6, "y": 0},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "count(steward_app_info{node=~\"$node\", enabled=\"true\"})",
          "instant": true,
          "legendFormat": "apps",
          "refId": "A"
        }
      ],
      "options": {
        "reduceOptions": {"values": false, "calcs": ["lastNotNull"], "fields": ""},
        "orientation": "auto", "textMode": "auto",
        "colorMode": "value", "graphMode": "none", "justifyMode": "auto"
      },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "color": {"mode": "thresholds"},
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "blue", "value": null}]
          }
        },
        "overrides": []
      }
    },
    {
      "id": 3,
      "title": "Apply Failures (1 h)",
      "description": "Total failed docker compose apply runs across all apps in the last hour.",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "sum(increase(steward_app_sync_total{node=~\"$node\", result=\"failed\"}[1h])) or vector(0)",
          "instant": true,
          "legendFormat": "failures",
          "refId": "A"
        }
      ],
      "options": {
        "reduceOptions": {"values": false, "calcs": ["lastNotNull"], "fields": ""},
        "orientation": "auto", "textMode": "auto",
        "colorMode": "background", "graphMode": "none", "justifyMode": "auto"
      },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "color": {"mode": "thresholds"},
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green", "value": null},
              {"color": "red",   "value": 1}
            ]
          }
        },
        "overrides": []
      }
    },
    {
      "id": 4,
      "title": "Last Reconcile Duration",
      "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 0},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "steward_reconcile_duration_seconds{node=~\"$node\"}",
          "instant": true,
          "legendFormat": "{{node}}",
          "refId": "A"
        }
      ],
      "options": {
        "reduceOptions": {"values": false, "calcs": ["lastNotNull"], "fields": ""},
        "orientation": "auto", "textMode": "auto",
        "colorMode": "value", "graphMode": "none", "justifyMode": "auto"
      },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "displayName": "${__field.labels.node}",
          "color": {"mode": "thresholds"},
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green",  "value": null},
              {"color": "yellow", "value": 30},
              {"color": "red",    "value": 120}
            ]
          }
        },
        "overrides": []
      }
    },
    {
      "id": 5,
      "title": "App Status",
      "description": "Per-app status snapshot: reconcile staleness, OutOfSync, Degraded, failures, and self-heal activity.",
      "type": "table",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 4},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "time() - steward_app_last_reconcile_timestamp_seconds{node=~\"$node\"}",
          "instant": true, "format": "table", "legendFormat": "", "refId": "A"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "max by (node, app) (steward_app_sync_status{node=~\"$node\", status=\"OutOfSync\"})",
          "instant": true, "format": "table", "legendFormat": "", "refId": "B"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "max by (node, app) (steward_app_health_status{node=~\"$node\", status=\"Degraded\"})",
          "instant": true, "format": "table", "legendFormat": "", "refId": "C"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "increase(steward_app_reconcile_total{node=~\"$node\", result=\"failed\"}[1h])",
          "instant": true, "format": "table", "legendFormat": "", "refId": "D"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "increase(steward_app_sync_total{node=~\"$node\", result=\"failed\"}[1h])",
          "instant": true, "format": "table", "legendFormat": "", "refId": "E"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "increase(steward_app_ooband_heal_total{node=~\"$node\"}[1h])",
          "instant": true, "format": "table", "legendFormat": "", "refId": "F"
        }
      ],
      "transformations": [
        {"id": "merge", "options": {}},
        {
          "id": "organize",
          "options": {
            "excludeByName": {
              "Time": true, "__name__": true, "job": true, "instance": true,
              "result": true, "status": true, "ref": true, "ref_type": true, "repo": true, "enabled": true
            },
            "renameByName": {
              "node": "Node",
              "app": "App",
              "Value #A": "Last Reconcile (s ago)",
              "Value #B": "OutOfSync",
              "Value #C": "Degraded",
              "Value #D": "Reconcile Failures (1h)",
              "Value #E": "Apply Failures (1h)",
              "Value #F": "OOB Heals (1h)"
            }
          }
        }
      ],
      "options": {"showHeader": true},
      "fieldConfig": {
        "defaults": {"custom": {"align": "left", "displayMode": "auto"}},
        "overrides": [
          {
            "matcher": {"id": "byName", "options": "Last Reconcile (s ago)"},
            "properties": [
              {"id": "unit", "value": "s"},
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "yellow", "value": 120}, {"color": "red", "value": 300}]
              }}
            ]
          },
          {
            "matcher": {"id": "byName", "options": "OutOfSync"},
            "properties": [
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]
              }}
            ]
          },
          {
            "matcher": {"id": "byName", "options": "Degraded"},
            "properties": [
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]
              }}
            ]
          },
          {
            "matcher": {"id": "byName", "options": "Reconcile Failures (1h)"},
            "properties": [
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]
              }}
            ]
          },
          {
            "matcher": {"id": "byName", "options": "Apply Failures (1h)"},
            "properties": [
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]
              }}
            ]
          },
          {
            "matcher": {"id": "byName", "options": "OOB Heals (1h)"},
            "properties": [
              {"id": "custom.displayMode", "value": "color-background"},
              {"id": "thresholds", "value": {
                "mode": "absolute",
                "steps": [{"color": "green", "value": null}, {"color": "yellow", "value": 1}, {"color": "red", "value": 5}]
              }}
            ]
          }
        ]
      }
    },
    {
      "id": 6,
      "title": "Sync Events (docker compose runs)",
      "description": "Each bar = a compose run triggered by a repo change during that 5-minute window.",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 12},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "increase(steward_app_sync_total{node=~\"$node\", result=\"success\"}[5m])",
          "legendFormat": "{{node}}/{{app}} ✓", "refId": "A"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "increase(steward_app_sync_total{node=~\"$node\", result=\"failed\"}[5m])",
          "legendFormat": "{{node}}/{{app}} ✗", "refId": "B"
        }
      ],
      "options": {
        "tooltip": {"mode": "multi", "sort": "none"},
        "legend": {"showLegend": true, "displayMode": "list", "placement": "bottom"}
      },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "color": {"mode": "palette-classic"},
          "custom": {"lineWidth": 2, "fillOpacity": 20, "drawStyle": "bars", "barAlignment": 0, "spanNulls": false}
        },
        "overrides": [
          {
            "matcher": {"id": "byFrameRefID", "options": "B"},
            "properties": [{"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}]
          }
        ]
      }
    },
    {
      "id": 7,
      "title": "Reconcile Duration",
      "description": "How long each full reconciliation run takes. Spikes indicate slow git fetches or large compose pulls.",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 12},
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
          "expr": "steward_reconcile_duration_seconds{node=~\"$node\"}",
          "legendFormat": "{{node}}", "refId": "A"
        }
      ],
      "options": {
        "tooltip": {"mode": "multi", "sort": "none"},
        "legend": {"showLegend": true, "displayMode": "list", "placement": "bottom"}
      },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "color": {"mode": "palette-classic"},
          "custom": {"lineWidth": 2, "fillOpacity": 10, "drawStyle": "line", "spanNulls": false},
          "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "green", "value": null}, {"color": "yellow", "value": 30}, {"color": "red", "value": 120}]
          }
        },
        "overrides": []
      }
    }
  ]
}
```

---

## Grafana alert rules

### Option A — provisioning file (recommended for Grafana deployed via Docker/Helm)

Place the file at `/etc/grafana/provisioning/alerting/steward.yaml`.

Replace `REPLACE_WITH_YOUR_PROMETHEUS_UID` with your datasource UID (find it at
**Connections → Data sources → your Prometheus → copy the UID from the URL**).

```yaml
apiVersion: 1
groups:
  - name: Steward
    orgId: 1
    folder: GitOps
    interval: 1m
    rules:

      - uid: steward-compose-failed
        title: "Steward — Compose apply failed"
        condition: threshold
        for: 0s
        labels:
          severity: critical
        annotations:
          summary: "Compose failed for {{ $labels.app }} on {{ $labels.node }}"
          description: "docker compose up returned non-zero for app {{ $labels.app }} on node {{ $labels.node }}. Check steward logs."
        data:
          - refId: query
            relativeTimeRange: {from: 300, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'increase(steward_app_sync_total{result="failed"}[5m])'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [0]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-reconcile-failures
        title: "Steward — Repeated reconcile failures"
        condition: threshold
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.app }} on {{ $labels.node }} has repeated reconcile failures"
          description: "More than 2 reconcile failures in 15 minutes for app {{ $labels.app }}. The stack repo may be unreachable or the compose file invalid."
        data:
          - refId: query
            relativeTimeRange: {from: 900, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'increase(steward_app_reconcile_total{result="failed"}[15m])'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [2]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-node-silent
        title: "Steward — Node not reporting"
        condition: threshold
        for: 0s
        labels:
          severity: critical
        annotations:
          summary: "Steward node {{ $labels.node }} has gone silent"
          description: "No reconciliation run seen for > 5 minutes on node {{ $labels.node }}. The steward container may be down."
        data:
          - refId: query
            relativeTimeRange: {from: 600, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'time() - steward_reconcile_last_timestamp_seconds'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [300]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: Alerting
        execErrState: Error
        isPaused: false

      - uid: steward-app-stale
        title: "Steward — App not reconciled"
        condition: threshold
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "App {{ $labels.app }} on {{ $labels.node }} has not been reconciled recently"
          description: "steward_app_last_reconcile_timestamp_seconds is > 5 minutes old for {{ $labels.app }}."
        data:
          - refId: query
            relativeTimeRange: {from: 600, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'time() - steward_app_last_reconcile_timestamp_seconds'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [300]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-health-degraded
        title: "Steward — App health degraded"
        condition: threshold
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Health degraded for {{ $labels.app }} on {{ $labels.node }}"
          description: "steward_app_health_status is Degraded for app {{ $labels.app }} for at least 5 minutes."
        data:
          - refId: query
            relativeTimeRange: {from: 300, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'steward_app_health_status{status="Degraded"}'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [0]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-app-outofsync
        title: "Steward — App remains out of sync"
        condition: threshold
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.app }} on {{ $labels.node }} is OutOfSync"
          description: "steward_app_sync_status has remained OutOfSync for at least 10 minutes."
        data:
          - refId: query
            relativeTimeRange: {from: 600, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'steward_app_sync_status{status="OutOfSync"}'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [0]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-control-repo-sync-failed
        title: "Steward — Control repo sync failed"
        condition: threshold
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "Control repo sync failed on {{ $labels.node }}"
          description: "steward_control_repo_sync_total{result=\"failed\"} increased in the last 15 minutes."
        data:
          - refId: query
            relativeTimeRange: {from: 900, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'increase(steward_control_repo_sync_total{result="failed"}[15m])'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [0]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false

      - uid: steward-manifest-parse-error
        title: "Steward — Manifest parse error"
        condition: threshold
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "Manifest parse error on {{ $labels.node }}"
          description: "steward_manifest_parse_errors_total increased in the last 5 minutes. A manifest file in the control repo is invalid and the app is being tracked as failed."
        data:
          - refId: query
            relativeTimeRange: {from: 300, to: 0}
            datasourceUid: REPLACE_WITH_YOUR_PROMETHEUS_UID
            model:
              expr: 'increase(steward_manifest_parse_errors_total[5m])'
              instant: true
              refId: query
          - refId: threshold
            datasourceUid: "-100"
            model:
              type: threshold
              expression: query
              refId: threshold
              conditions:
                - evaluator: {type: gt, params: [0]}
                  operator: {type: and}
                  reducer: {type: last, params: []}
                  query: {params: [query]}
                  type: query
        noDataState: OK
        execErrState: Error
        isPaused: false
```

### Option B — manual creation via UI

Go to **Alerting → Alert rules → New alert rule** and use these PromQL expressions:

| Alert | Expression | Threshold | Severity |
|---|---|---|---|
| Compose apply failed | `increase(steward_app_sync_total{result="failed"}[5m])` | `> 0` | critical |
| Repeated reconcile failures | `increase(steward_app_reconcile_total{result="failed"}[15m])` | `> 2` | warning |
| Node not reporting | `time() - steward_reconcile_last_timestamp_seconds` | `> 300` | critical — set **No data** → Alerting |
| App not reconciled | `time() - steward_app_last_reconcile_timestamp_seconds` | `> 300` | warning |
| App health degraded | `steward_app_health_status{status="Degraded"}` | `> 0` | warning |
| App remains out of sync | `steward_app_sync_status{status="OutOfSync"}` | `> 0` for 10m | warning |
| Control repo sync failed | `increase(steward_control_repo_sync_total{result="failed"}[15m])` | `> 0` | warning |
| Manifest parse error | `increase(steward_manifest_parse_errors_total[5m])` | `> 0` | warning |
