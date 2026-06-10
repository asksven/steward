# steward — future development plan

Goal: _like ArgoCD but for Docker Compose_.

Items are grouped by four goals and ordered roughly by impact within each group.
Each item carries a complexity rating and — where the design is not yet settled — an explicit
list of open questions that must be discussed and decided before implementation begins.

---

## Goal 1 — ArgoCD parity: missing features

These items add capabilities that ArgoCD has and steward currently lacks entirely.
Without them steward is a reconciler but not a GitOps controller in the ArgoCD sense.

---

### 1.1 Sync status per app — OutOfSync / Synced / Unknown

**ArgoCD equivalent:** `Application.status.sync.status`

**Problem:** Steward has no persistent, queryable sync status per app. A reconcile run either
succeeds or fails, but there is no durable answer to "is app X currently in sync with Git?"
This is the single most recognisable concept in ArgoCD and its absence is the biggest gap in
the "like ArgoCD" claim.

**Proposed states:**

| Status | Meaning |
|---|---|
| `Synced` | Deployed SHA matches the remote ref SHA |
| `OutOfSync` | Remote ref is ahead of deployed SHA, or live state diverges from compose spec |
| `Unknown` | Cannot determine — git fetch failed, compose ps failed, or no prior deploy |
| `Disabled` | `enabled: false` in manifest — explicitly not managed |

**Proposed solution:** After each reconcile cycle, persist the sync status for every app in
`STEWARD_DATA_DIR/steward.db` (SQLite — see item 3.1). Expose it as a Prometheus metric label
`steward_app_sync_status{app, node, status}` (gauge, value always 1, one series per status).
Observed state is surfaced via Prometheus + SQLite only — it is **not** written back to the
control repo (item 2.1 was removed; see its entry for rationale).

**Open questions:** resolved.
- What is the correct `OutOfSync` definition when `sync_policy: manual` is set (item 1.3)?
  Should steward report `OutOfSync` but never act, or use a distinct `Pending` status?
- When a manifest is newly added (first-ever deploy), should the initial status be `Unknown`
  or `OutOfSync`? ArgoCD uses `Unknown` until the first comparison completes.
- Should `Disabled` be a sync status or a separate field in the state? Keeping it separate
  avoids conflating operational state with sync state.

**Decisions (2026-05-23):**
- `sync_policy: manual` reports `OutOfSync` and skips apply.
- Newly added apps start as `Unknown` until a successful comparison is possible.
- `Disabled` is exposed as a sync status value for operational clarity in metrics.

**Implementation status:**
- Steward now persists per-app sync status in SQLite (`Synced`, `OutOfSync`, `Unknown`, `Disabled`).
- `/metrics` now exposes `steward_app_sync_status{app,node,status}` as a one-hot gauge per status label.
- Item 1.1 is considered complete: observed sync state is exposed via Prometheus + SQLite. The
  former "remaining part" (control-repo status writeback via item 2.1) was removed by the
  2026-06-10 GitOps write-path decision and is no longer part of 1.1's scope.

**Complexity:** Medium — requires item 3.1 (SQLite state) as a foundation, but the status logic
itself is straightforward once that exists.

**Depends on:** 3.1

---

### 1.2 Health status per app — Healthy / Degraded / Progressing / Unknown

**ArgoCD equivalent:** `Application.status.health.status`

**Problem:** Steward only checks whether `docker compose up` exited 0, not whether the
resulting containers are actually running and healthy. A compose up can succeed while all
containers immediately crash-loop (e.g. bad env var, missing mount). Sync status and health
status are separate concerns; conflating them hides real failures.

**Proposed states:**

| Status | Meaning |
|---|---|
| `Healthy` | All non-oneshot services are `running` per `docker compose ps` |
| `Degraded` | One or more services are `exited`, `dead`, or in a restart loop |
| `Progressing` | Compose up just ran; health check window not yet elapsed |
| `Unknown` | Cannot determine — compose ps failed or no deploy has occurred |

**Proposed solution:** After a successful `compose up`, enter a `Progressing` window
(configurable, default 30s). After the window, run `docker compose ps --format json` and
classify each service. A service is healthy if `State=running`. Oneshot services (`restart: no`
or `restart: on-failure` with exit 0) are excluded from the health check. Persist the health
status alongside sync status in SQLite and expose as
`steward_app_health_status{app, node, status}`.

**Open questions:** resolved.
- What is the right `Progressing` window default? 30 seconds is reasonable for most homelab
  stacks but may be too short for slow-starting services (databases, Jellyfin indexing). Should
  this be configurable at the manifest level (`health_check_delay_seconds`)?
- How should steward handle services with no `healthcheck:` defined in the compose file?
  Docker reports `State=running` as soon as the process starts, even if it is not ready.
  Options: (a) accept `running` as healthy regardless, (b) add an optional `health_check_cmd`
  field to the manifest, (c) treat services without a healthcheck as `Unknown` health.
- Should a `Degraded` health status trigger an automatic re-sync? Or only alert? This interacts
  with item 1.3 (sync policy).
- How do we classify a service that is restarting? `docker compose ps` may show `running`
  between restarts. Should we track restart count delta as a `Degraded` signal?

**Decisions (2026-05-23):**
- `Progressing` default delay is 30 seconds.
- Delay is configurable per app via manifest field `health_check_delay_seconds`.
- Services without Docker healthcheck are treated as healthy when compose state is `running`.
- `Degraded` triggers automatic re-apply only when `sync_policy=auto`.
- Services in `restarting` state are classified as `Degraded`.

**Implementation status:**
- Manifest parser accepts `health_check_delay_seconds` and validates integer bounds.
- Reconcile persists `health_status` per app in SQLite and updates it on each cycle.
- Health status transitions are implemented: `Progressing` after successful apply, then `Healthy|Degraded|Unknown` from `docker compose ps --format json` after delay.
- `/metrics` exposes `steward_app_health_status{app,node,status}` as one-hot gauge labels.
- Policy gating is enforced: degraded auto-reapply runs only for apps with `sync_policy=auto`.

**Complexity:** Medium — requires parsing `docker compose ps --format json` output and defining
the health classification rules. The `Progressing` window adds a timing concern to the otherwise
stateless cron loop.

**Depends on:** 1.1 (sync status framework), 3.1 (SQLite state)

---

### 1.3 Sync policy per app — auto / manual

**ArgoCD equivalent:** `Application.spec.syncPolicy`

**Problem:** Every app is currently reconciled automatically on every cycle. There is no way to
say "detect drift and alert, but require a human to trigger the actual sync." For production-like
nodes or apps managing data (databases, media servers), automatic sync on every git change is
too aggressive. Without a manual sync policy option, the "like ArgoCD" claim is incomplete for
any operator who uses ArgoCD's manual sync mode.

**Proposed manifest field (v2 schema):**

```yaml
sync_policy: auto       # default — reconcile automatically (current behaviour)
# sync_policy: manual   # detect and report OutOfSync but do not apply
```

**Proposed solution:** When `sync_policy: manual`, steward performs the git fetch and SHA
comparison as normal and updates sync status to `OutOfSync` if drift is detected, but does not
call `docker compose up`. The only way to trigger a sync in manual mode is via a future
`steward sync <app>` CLI command (out of scope for this item) or by temporarily setting
`sync_policy: auto` in the manifest.

**Open questions:** resolved.
- Should `manual` mode also suppress the self-heal trigger (item 1.4)? i.e. if a container
  crashes, does `sync_policy: manual` mean steward never heals it either? Likely yes — the
  operator has opted out of automatic changes. But this should be an explicit decision.
- Is `sync_policy` a manifest-level field (per app) or a node-level env var (per node)?
  Per-app is more flexible; per-node is simpler. Could be both with per-app overriding the
  node default.
- What notification behaviour should accompany `OutOfSync` in manual mode? Should it trigger
  the failure notification (item 3.3) or a distinct `drift_detected` notification type?

**Decisions (2026-05-23):**
- `sync_policy` is manifest-level (per app).
- Default is `auto` to preserve current behavior.
- `manual` performs fetch/compare and reports `OutOfSync`, but does not call `docker compose up`.
- `manual` currently suppresses any automatic apply path; future self-heal logic (item 1.4) must honor this gate.

**Implementation status:**
- Manifest parser accepts `sync_policy` and validates `auto|manual`.
- Reconcile flow enforces policy: `manual` skips apply, `auto` applies when out of sync.
- Sync status/metrics reflect manual drift as `OutOfSync` without incrementing compose sync counters.
- Tests cover both policy branches (`manual` skip and `auto` apply).

**Complexity:** Low — the check/sync split from item 2.3 makes this straightforward once that
refactor is done.

**Depends on:** 2.3 (check/sync phase split)

---

### 1.4 Live state drift detection / self-healing

**ArgoCD equivalent:** `syncPolicy.automated.selfHeal`

**Problem:** Steward only acts when the git SHA advances. If containers are stopped manually,
crash-loop, or are removed outside of steward, no action is taken until the next git commit.
This is a fundamental gap vs ArgoCD's self-healing model — the system is not continuously
converging toward desired state, only reacting to Git changes.

**Proposed solution:** On each reconcile cycle, after the git check, query the live state of
the stack (`docker compose ps --format json`) and compare it against the expected services
from the compose file. If any required service is missing or stopped, trigger `compose up`
regardless of whether git changed. The git SHA check and the live state check become two
independent triggers for the same sync action.

**Open questions:** resolved.

**Decisions (2026-05-23):**
- Expected services are sourced from `docker compose config --services` (desired-state source of truth).
- Intentionally stopped services are treated as drift; GitOps remains strict unless app is disabled in manifest.
- Self-heal is gated by `sync_policy` and only applies when `sync_policy=auto`.
- Restart-loop thresholding is deferred; first version has no additional threshold gate.

**Implementation status:**
- Reconcile now checks live drift even when git SHA is unchanged.
- Drift between expected and live services triggers self-heal apply for `sync_policy=auto`.
- For `sync_policy=manual`, live drift is recorded and reported without apply.
- Self-heal outcomes are recorded in operation history with trigger `self_heal`.

**Complexity:** Medium — requires parsing compose ps output and defining expected vs actual
state comparison. Restart-loop detection adds meaningful complexity.

**Depends on:** 1.2 (health status, for the ps parsing logic)

---

### 1.5 Operation history and audit log

**ArgoCD equivalent:** `Application.status.history`

**Problem:** There is no durable record of what was deployed, when, from which SHA, and whether
it succeeded. You cannot answer "when did this app last change?" or "was the system ever in sync
with commit abc123?" — which are the core auditability promises of GitOps. The Prometheus metrics
give aggregates (counters, timestamps) but not an event ledger.

**Why not Prometheus for this?** Prometheus is an observability sink — it answers "what is
happening now / recently?" via samples and aggregates. It is not durable (samples are dropped
after retention), not queryable per-event, and not readable by steward itself. It is the right
tool for fleet-wide alerting but the wrong tool for per-app operation history.

**Proposed solution:** Store an append-only operation log in SQLite (`STEWARD_DATA_DIR/steward.db`).
On every sync attempt, insert one row:

```sql
CREATE TABLE operations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  app            TEXT    NOT NULL,
  node           TEXT    NOT NULL,
  started_at     TEXT    NOT NULL,   -- ISO 8601
  completed_at   TEXT,
  from_sha       TEXT,               -- NULL on first deploy
  to_sha         TEXT    NOT NULL,
  sync_status    TEXT    NOT NULL,   -- Synced | Failed | Skipped
  health_status  TEXT,               -- Healthy | Degraded | Unknown
  duration_s     REAL,
  message        TEXT                -- error message if failed
);
```

Retain the last N rows per app (configurable, default 50). Expose history via a future
`steward history <app>` CLI command or the `/status` HTTP endpoint alongside `/metrics`.

**Open questions:** resolved.
- Should the operation log also record drift-detection events (i.e. a row when `OutOfSync` is
  detected but no sync is triggered because `sync_policy: manual`)? This would make the log a
  complete audit trail rather than only a sync log.
- What is the right retention default? 50 rows per app is lightweight; 200 gives ~3 months of
  history at one sync per hour. Should retention be time-based (90 days) rather than count-based?
- Should SQLite be the only state store, or should JSON remain as a simpler alternative for
  operators who want human-readable state without a database tool? The recommendation is SQLite
  only — the complexity of maintaining two state formats outweighs the convenience of JSON.
- The SQLite file lives in `STEWARD_DATA_DIR`. Should it be a separate file (`steward.db`) or
  merged with any existing state JSON? Recommendation: replace the JSON state file with SQLite
  entirely as part of this item.

**Decisions (2026-05-23):**
- Operation history includes manual-mode drift events (apply skipped) for complete auditability.
- Retention is time-based and keeps the last 90 days of operation rows.
- SQLite is the only state store (`steward.db`), with no JSON alternative.

**Implementation status:**
- Git-change sync attempts and self-heal events are appended to the `operations` table.
- Manual drift events are recorded with `sync_status=Skipped` and descriptive messages.
- Save path prunes operation rows older than 90 days for the current node.

**Complexity:** Medium — SQLite is in the Python stdlib (`sqlite3`), so no new dependency.
The schema is simple. The main work is migrating away from the existing JSON state file and
wiring the insert into the reconcile loop.

---

## Goal 2 — GitOps idiom: redesign non-idiomatic features

These items correct behaviours that work today but violate GitOps principles or introduce
subtle correctness problems that will compound as steward matures.

---

### 2.1 Observed state writeback to control repo — REMOVED (2026-06-10)

**Status: removed.** This feature was implemented (2026-05-23) and then removed on 2026-06-10
after it caused a multi-node failure and was found to contradict GitOps principles. See
"Decision: drop git status writeback" below for the full rationale and removal scope.

**Why it was removed (summary):**
- **Not a GitOps principle.** The original premise — "a pure GitOps system writes observed state
  back to the same repo" — is not one of the OpenGitOps principles (declarative, versioned &
  immutable, pulled, continuously reconciled), all of which concern *desired* state. Git is the
  source of truth for *what should be*, not *what is*.
- **The ArgoCD analogy argues against it.** ArgoCD writes `.status` to the Application CRD in
  **etcd**, surfaced via API/UI/metrics — it never commits status into the git repo.
- **It broke in production.** With more than one node pushing `nodes/<host>/status.json` to the
  same `main`, concurrent pushes were rejected (non-fast-forward), leaving a divergent local
  commit that poisoned the next `git pull` (`exit 128`, divergent branches) and wedged the node.
- **The conflation was the root cause.** The reconciler mutating the same ref it reads desired
  state from is exactly what produced the deadlock.

Observed state is now exposed **only** via Prometheus `/metrics` + local SQLite (items 1.1, 1.2,
1.5, 3.1). The control repo is desired-state-only and steward treats it as read-only.

**Superseded decisions (2026-05-23, no longer in effect):** single read/write token; write
`status.json` only on content change; direct gitpython commit/push to `CONTROL_REPO_BRANCH`;
writeback failure → `partial_failure`.

---

### 2.2 GitOps write-path contract — Git is the only write path

**GitOps principle:** The only supported way to change the desired state of a running system is
via a Git commit. Out-of-band changes are drift by definition and should be healed, not
accommodated.

**Problem:** This contract is implicit in steward today but is not stated, enforced, or
surfaced as a status. An operator who edits a compose file directly on the node, or runs
`docker compose up` manually, is making an out-of-band change that steward will silently
overwrite on the next cycle. There is no signal that this happened.

**Proposed solution:**
- Document the write-path contract explicitly in the README as a first-class operational rule.
- When self-heal (item 1.4) detects and corrects an out-of-band change, log it at `WARNING`
  level with a distinct message (`out-of-band drift detected and healed for app <name>`) and
  increment a dedicated metric `steward_app_ooband_heal_total{app, node}`.
- Consider a `dry_run: true` manifest field or `STEWARD_DRY_RUN=true` env var that makes
  steward detect and report all drift (including out-of-band) without ever calling
  `docker compose up`. This is the audit / read-only mode that complements `sync_policy: manual`.

**Open questions:** resolved.

**Decisions (2026-05-23):**
- `dry_run` is a global node-level env var: `STEWARD_DRY_RUN=true`.
- Out-of-band heal visibility uses both operation history (`trigger=self_heal`) and
  dedicated metric `steward_app_ooband_heal_total{app,node}`.
- Out-of-band auto-heal logs a warning with explicit message text for operator visibility.

**Implementation status:**
- `STEWARD_DRY_RUN` now disables all compose apply actions while keeping drift detection,
  status updates, notifications, and operation history.
- Auto-heal writes warning logs for out-of-band drift healed by steward.
- Metrics now expose `steward_app_ooband_heal_total{app,node}`.
- Out-of-band heal operations are already recorded with `trigger=self_heal`.

**Complexity:** Low to Medium — first usable contract is implemented; future work can add
per-app dry-run if needed.

---

### 2.3 Reconcile / sync phase split

**Problem:** `reconcile_app` currently does both drift detection and apply in one function. The
metric names hint at the distinction (`steward_app_sync_total` vs `steward_app_reconcile_total`)
but the code conflates the two phases. This makes it impossible to implement `sync_policy: manual`
cleanly (item 1.3), hard to reason about which phase failed, and difficult to add the
`Progressing` health state (item 1.2) without restructuring the function anyway.

**Proposed solution:** Split `reconcile_app` into two clearly named phases:

- `check_app(app, repo, state) -> SyncStatus` — git fetch, SHA comparison, live state check
  (item 1.4), return `Synced | OutOfSync | Unknown`
- `sync_app(app, repo, state) -> SyncResult` — compose up, update deployed SHA, enter
  `Progressing` window, run health check, return `success | failed`

`reconcile_app` becomes a thin orchestrator:

```python
status = check_app(app, repo, state)
if status == OutOfSync and app.sync_policy == "auto":
    result = sync_app(app, repo, state)
```

**Open questions:** resolved.

**Decisions (2026-05-23):**
- `check_app` and `sync_app` return result objects; `reconcile_app` is the only place that
  persists metrics/state updates. This keeps phase logic testable and side effects centralized.
- Git fetch is an explicit step before `check_app`, so `check_app` remains a pure
  local-vs-remote SHA comparison over already-fetched refs.
- Scope allows only low-risk tweaks needed to keep phase boundaries coherent; no new user-facing
  behavior is introduced as part of 2.3 itself.

**Implementation status:**
- Refactor landed in `steward.py` with `fetch_ref` -> `check_app` -> `sync_app` orchestration.
- Existing `sync_repo` remains as a compatibility wrapper for call sites that still need the
  old tri-state return (`True` updated / `False` up-to-date / `None` error).

**Complexity:** Low-medium — refactor only, no new external behaviour. This is a prerequisite
for items 1.2, 1.3, and 1.4 and should be done first within Goal 1.

**Prerequisite for:** 1.2, 1.3, 1.4

---

### 2.4 Manifest schema v2 — rename `env_file` to `compose_env_file`

**Problem:** The manifest `env_file` field is passed as `docker compose --env-file`, which
controls compose variable substitution — not container environment variables. This collides with
Docker Compose's native `env_file:` service key, which injects variables into the container.
The naming is a silent footgun for any operator familiar with Docker Compose.

**Proposed solution:** Introduce a v2 manifest schema. In v2, `env_file` is renamed to
`compose_env_file`. In v2, also add `sync_policy` (item 1.3) and `pull_policy` (item 3.2).
The reconciler accepts both v1 and v2 manifests; v1 `env_file` emits a deprecation warning in
the log. A future version drops v1 support. The v2 bump is also the right moment to add JSON
Schema validation so manifest errors surface clearly.

**Proposed v2 manifest:**

```yaml
version: 2
name: arr
repo: https://github.com/you/arr-stack
ref:
  branch: main
path: .
compose_file: docker-compose.yml
compose_env_file: /git/envs/arr.env   # renamed from env_file
sync_policy: auto                      # new in v2
pull_policy: always                    # new in v2
enabled: true
```

**Open questions:** resolved.

**Decisions (2026-05-23):**
- Manifest v2 is now active and bundled with `sync_policy` (item 1.3) and `pull_policy` (item 3.2).
- Steward accepts both `version: 1` and `version: 2` manifests during migration.
- `env_file` remains accepted as a compatibility shim but emits a deprecation warning that points users to `compose_env_file`.
- `compose_env_file` and `env_file` are mutually exclusive in one manifest.

**Implementation status:**
- `steward.py` parser now supports schema v2 and validates `sync_policy`/`pull_policy` values.
- `compose_env_file` is supported and mapped to compose `--env-file`; deprecated `env_file` still works with warning.
- Tests cover v2 parsing, policy validation, defaults, and mutual exclusivity.

**Complexity:** Low-medium — schema parsing change plus deprecation shim.

---

### 2.5 Explicit `--project-name` for all compose invocations

**Problem:** Docker Compose derives the project name from the working directory name. This
works today because repos are cloned into `STACKS_DIR/<app.name>`, but it is an implicit
assumption. Any future change to the directory layout (e.g. supporting multiple compose files
per repo) would silently create a new project name, orphaning the old containers.

**Proposed solution:** Always pass `--project-name <app.name>` to every `docker compose`
invocation (`up`, `ps`, `pull`, `down`). This makes the project name an explicit contract
matching the manifest `name` field and decouples it from the directory structure.

**Open questions:** None — this is unambiguously correct.

**Implementation status:**
- All compose invocations now pass `--project-name <app.name>`.
- Project name is explicit for `up`, `ps`, and `config --services` paths.
- Tests validate project-name flag usage for direct compose and self-update helper flows.

**Complexity:** Trivial.

---

## Goal 3 — ArgoCD parity: deepen the claim

These items bring steward closer to feature parity with ArgoCD in areas that directly support
the "like ArgoCD but for Docker Compose" positioning.

---

### 3.1 SQLite as the single state store

**Problem:** The current JSON state file is not structured, not queryable, and not safe for
concurrent access. As steward gains sync status, health status, and operation history, the JSON
file becomes increasingly unwieldy and the lack of atomicity becomes a correctness risk.

**Proposed solution:** Replace the JSON state file with a SQLite database at
`STEWARD_DATA_DIR/steward.db`. SQLite is in the Python stdlib (`import sqlite3`), adds no
dependency, and provides atomic writes, a proper schema, and queryability. The schema covers
three tables:

```sql
-- Current state per app (upserted on every reconcile cycle)
CREATE TABLE app_state (
  app            TEXT PRIMARY KEY,
  node           TEXT NOT NULL,
  sync_status    TEXT NOT NULL,
  health_status  TEXT NOT NULL,
  deployed_sha   TEXT,
  remote_sha     TEXT,
  last_checked   TEXT NOT NULL,
  last_synced    TEXT
);

-- Append-only operation log (see item 1.5)
CREATE TABLE operations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  app            TEXT NOT NULL,
  node           TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  completed_at   TEXT,
  trigger        TEXT NOT NULL,  -- git_change | self_heal | manual
  from_sha       TEXT,
  to_sha         TEXT NOT NULL,
  sync_status    TEXT NOT NULL,
  health_status  TEXT,
  duration_s     REAL,
  message        TEXT
);

-- Manifest metadata (upserted when control repo changes)
CREATE TABLE app_manifest (
  app            TEXT PRIMARY KEY,
  node           TEXT NOT NULL,
  repo           TEXT NOT NULL,
  ref_type       TEXT NOT NULL,  -- branch | tag
  ref_value      TEXT NOT NULL,
  sync_policy    TEXT NOT NULL,
  enabled        INTEGER NOT NULL,
  updated_at     TEXT NOT NULL
);
```

**Open questions:** resolved.

**Decisions (2026-05-23):**
- Metrics state is merged into SQLite and `metrics_server.py` reads SQLite directly.
- Migration mode is clean-slate: old JSON state is ignored (no auto-import, no manual import flow).

**Implementation status:**
- `steward.py` now persists reconcile/app state in `steward.db` and initializes SQLite schema on demand.
- `metrics_server.py` now reads metrics input from SQLite instead of `metrics/state.json`.
- A one-time startup info log is emitted when a legacy `metrics/state.json` file is detected,
  explicitly stating that SQLite is authoritative and starts fresh by design.

**Complexity:** Medium — the schema is simple but migrating `metrics_server.py` and the
reconcile loop to write SQLite instead of JSON requires touching several parts of the codebase.
This item is a foundation for items 1.1, 1.2, and 1.5 and should be done alongside them.

**Prerequisite for:** 1.1, 1.2, 1.5

---

### 3.2 Smarter image pulling — configurable pull policy

**ArgoCD equivalent:** Image updater / sync options

**Problem:** `--pull always` is passed on every `compose up`, which re-pulls every image in the
stack whenever any file in the repo changes — including non-image changes like README edits.
This is wasteful and can cause unintended image upgrades mid-reconcile.

**Proposed manifest field (v2 schema):**

```yaml
pull_policy: always    # default — current behaviour
# pull_policy: missing # only pull if image not present locally
# pull_policy: never   # never pull (use whatever is cached)
```

A future enhancement is a separate `digest` pull policy that runs `docker compose pull` first,
compares digests, and only restarts services whose image actually changed. This is the most
correct behaviour but adds meaningful complexity and is deferred.

**Open questions:** resolved.

**Decisions (2026-05-23):**
- `pull_policy` is a per-app manifest field.
- Default remains `always` for backward compatibility with existing behavior.

**Implementation status:**
- `run_compose` now passes `--pull <pull_policy>` from each app manifest.
- Self-update helper compose invocation also uses the app-level `pull_policy`.
- Tests validate that compose commands use configured pull policy values.

**Complexity:** Low — adding the field to manifest parsing and passing it to the compose
invocation is trivial. Intended to land as part of the v2 schema bump (item 2.4).

---

### 3.3 Failure notifications

**ArgoCD equivalent:** Notification controller

**Problem:** Reconciliation and health failures are visible in logs and metrics but there is
no active notification. A node can be failing silently for the window between Prometheus scrapes
and alert evaluation. For a homelab without 24/7 Grafana monitoring, this window can be hours.

**Proposed solution:** Add an optional `STEWARD_NOTIFY_URL` env var. On any `sync_status=Failed`
or `health_status=Degraded` event, POST a JSON payload to the URL. The payload format should be
compatible with ntfy, Gotify, and generic Slack/Discord incoming webhooks via a configurable
template.

**Open questions:** resolved.

**Decisions (2026-05-23):**
- Notification target supports both a node-wide default (`STEWARD_NOTIFY_URL`) and per-app manifest override (`notify_url`).
- Manual-mode `OutOfSync` sends `drift_detected` notifications.
- Notifications are intentionally not de-duplicated; bad states notify on every reconcile cycle.

**Implementation status:**
- Steward posts JSON webhook notifications for `sync_failed`, `health_degraded`, and `drift_detected` events.
- Payload includes app, node, sync/health status, SHA context, and message.
- Delivery uses per-app override first, then global default.

**Complexity:** Low — a `requests.post` call with a JSON payload. The de-duplication logic
(tracking previous status to detect transitions) requires the SQLite state store (item 3.1).

**Depends on:** 3.1 (for transition detection)

---

## Goal 4 — Engineering foundations

These items are not visible to end users but are prerequisites for sustainable development:
reproducible builds, a safety net for refactoring, and automated code quality checks.
They are independent of Goals 1–3 and can be worked in parallel.

---

### 4.1 Python dependency management via uv + requirements.txt

**Problem:** `gitpython` and `pyyaml` are installed in the Dockerfile with
`pip install --no-cache-dir` without pinned versions. Builds are not reproducible — a new
release of either package can silently change behaviour between two image builds. Dependabot
cannot track transitive dependency versions pinned in a lockfile.

**Proposed solution:**
- Adopt **uv** as the package manager for local dev, CI, and the Docker build.
- Add a `requirements.in` listing direct dependencies unpinned: `gitpython`, `pyyaml`.
- Generate a committed `requirements.txt` with fully pinned, platform-resolved versions via
  `uv pip compile`. Dependabot watches it via `package-ecosystem: pip`.
- In the Dockerfile, replace `pip install` with `uv pip install --system -r requirements.txt`.
- In CI (GitHub Actions), replace pip invocations with `uv`.

**Open questions:** resolved.

**Decisions (2026-05-23):**
- Project uses uv-first dependency flow with committed `requirements.in`, `requirements.txt`, and `uv.lock`.
- Docker build installs runtime deps via `uv pip install --system -r requirements.txt`.
- CI jobs use uv for environment setup and command execution.

**Implementation status:**
- Runtime deps are pinned in `requirements.txt` and managed from `requirements.in`.
- Dockerfile uses uv-based install path instead of direct pip dependency installs.
- CI workflow uses uv in both lint and test jobs.
- Dependabot includes weekly `pip` updates for root requirements files.

**Dependabot config:** add a `pip` entry to `.github/dependabot.yml` pointing at `requirements.txt`.

**Complexity:** Low — mostly mechanical.

---

### 4.2 Test suite

**Problem:** steward has no automated tests. Changes to manifest parsing, git sync logic, or
compose invocation are only validated by running the full agent on a live node, which makes
iteration slow and regressions likely. This becomes a blocker once Goal 1 refactoring begins
(item 2.3 phase split, item 3.1 SQLite migration).

**Proposed test surface:**

| Area | What to test | Approach |
|---|---|---|
| Manifest parsing | valid manifests, missing fields, both-branch-and-tag, bad version, v1/v2 schema | `pytest` with inline YAML strings |
| `sync_repo` tri-state | updated / up-to-date / error → correct return value | mock `Repo` + `GitCommandError` |
| `check_app` / `sync_app` phase split | OutOfSync triggers sync, Synced skips, manual policy skips | mock `sync_repo`, `run_compose` |
| `spawn_compose_helper` | host path resolution, correct `-v` / `-f` args, fallback on missing image | mock `_container_mounts`, `subprocess.run` |
| `_resolve_host_path` | bind mount, named volume, no match → None | mock `_container_mounts` |
| Metrics counters | `_inc` increments correct nested keys | pure unit test, no mocks needed |
| SQLite state | upsert idempotent, operation log append-only, row count respects retention | in-memory SQLite (`:memory:`) |

**Tooling:** `pytest` + `pytest-mock` (or stdlib `unittest.mock`). Run via `uv run pytest`.

**CI:** add a `test` job to `build.yml` that runs before `build-and-push` and blocks it on
failure.

**Implementation status:**
- Test suite is in place with focused coverage for parser, reconcile logic, compose integration behavior, and SQLite state.
- CI `test` job runs `uv run pytest -v --tb=short` and blocks image builds on failures.

**Complexity:** Medium — the git and subprocess interactions need careful mocking, but the
functions are well-isolated and testable.

---

### 4.3 Linting

**Problem:** There is no automated style or correctness check for the Python code or the
Dockerfile. Issues are only caught at runtime on a live node.

**Proposed solution:**
- **Python:** `ruff` (replaces flake8 + isort + pyupgrade in one tool; fast, uv-compatible).
  Run via `uv run ruff check steward.py metrics_server.py`. Config in `pyproject.toml`
  `[tool.ruff]` (line length, target Python version).
- **Dockerfile:** `hadolint` (industry-standard Dockerfile linter). Run in CI via the
  `hadolint/hadolint-action` GitHub Action.

**CI:** add a `lint` job to `build.yml` (parallel to `test`, both blocking `build-and-push`).

**Implementation status:**
- Ruff config is defined in `pyproject.toml` and enforced in CI.
- CI `lint` job runs `uv run ruff check steward.py metrics_server.py tests`.
- Hadolint is enabled in CI and checks the Dockerfile on each run.

**Complexity:** Low — mostly adding config files and CI steps.

## Already implemented

### ✓ GitOps self-update via Dependabot

Deployment `docker-compose.yml` stays in the steward source repo; Dependabot PRs update the
pinned image tag. Bootstrap is a one-time manual clone of the steward repo into
`STEWARD_DATA_DIR/stacks/steward`. From that point steward manages its own updates via the
normal reconcile loop. `AGENT_IMAGE` env var retained as a testing override. `self_update()`
function, the hourly self-update cron entry, and `steward_self_update_total` metric were
removed.

### ✓ Self-update helper container

When steward detects it is reconciling its own stack (`app.name == AGENT_CONTAINER_NAME`), it
spawns a short-lived helper container via the Docker socket instead of calling
`docker compose up -d` directly. The helper uses a 5-second delay to allow the old container to
exit cleanly. Key implementation decisions: the running steward image is used as the helper
image (already contains `docker-cli` + `docker-cli-compose`); host paths are resolved via
`_resolve_host_path()` which translates container paths to host paths using `docker inspect`
mounts; falls back to direct `compose up` if path resolution fails.

---

## Goal 6 — Improve security: git credential handling

Steward currently has two mechanisms for authenticating to git repositories (control repo and
app repos):

1. **SSH keys** — bind-mounted from the host's `~/.ssh/` into the container via
   `/root/.ssh-host/` staging directory, then copied into the container user's SSH home with
   corrected ownership/permissions.
2. **HTTPS tokens** — embedded directly in the repo URL (e.g.
   `https://oauth2:<token>@github.com/...`), stored either in the `.env` file next to
   `docker-compose.yml` or — if used in app manifests — potentially committed to the control
   repo itself.

Both approaches present security concerns that will cause pushback from security-conscious
users and limit adoption in team/enterprise environments.

---

### 6.1 Security analysis — current state

**Problem 1 — SSH key exposure via host filesystem access:**

The current model requires steward to read private SSH keys from the host's `~/.ssh/` directory
(bind-mounted read-only). This is problematic because:

- The `~/.ssh/` directory typically contains keys for *all* services the user authenticates to
  (not just git), violating the principle of least privilege.
- If an attacker compromises the steward container, they gain access to all mounted keys.
- The keys are copied into the container filesystem at startup (by `entrypoint.sh`) and persist
  there for the container's lifetime — not in-memory-only.
- There is no key rotation mechanism; keys are static for the container lifetime.
- Sharing host SSH identity with a container running as a different UID is operationally fragile
  and requires the documented multi-step SSH config workaround.

**Problem 2 — HTTPS tokens in URLs (control repo and app repos):**

- For the control repo: the token is in `CONTROL_REPO_URL` env var, sourced from `.env` on the
  host. This is acceptable if `.env` is properly secured, but the token is exposed to any
  process that inspects the container environment (`docker inspect`, `/proc/*/environ`).
- For app repos: if a user defines `repo: https://oauth2:<token>@github.com/...` in a manifest
  YAML file in the control repo, **the token is committed to git** — a well-known antipattern
  that exposes credentials in repository history permanently.
- Tokens embedded in URLs can leak into logs, error messages, and metrics. Steward already
  mitigates this with `strip_url_credentials()` for metrics, but other code paths (GitPython
  exceptions, git CLI stderr) may still leak.

**Problem 3 — No separation between control-plane and data-plane credentials:**

There is no distinction between the credential used to fetch the control repo and the
credentials used to fetch individual app repos. In a multi-repo setup, a single set of keys
or tokens must have access to all repositories — violating least-privilege.

---

### 6.2 Proposed improvements

The following improvements are ordered by impact and feasibility for the steward deployment
model (single-node Docker Compose, no Kubernetes/Swarm requirement).

---

#### 6.2.1 Support Docker Compose secrets for credential injection

**Industry standard:** Docker Compose `secrets` top-level element (file-based secrets mounted
to `/run/secrets/<name>`, read-only, in-memory tmpfs on Linux).

**Proposed solution:** Accept credentials via file references rather than environment variables
or bind-mounted SSH directories:

```yaml
# docker-compose.override.yml
services:
  steward:
    secrets:
      - git_ssh_key
      - control_repo_token

secrets:
  git_ssh_key:
    file: ./steward-deploy-key   # dedicated deploy key, NOT ~/.ssh/id_rsa
  control_repo_token:
    file: ./github-token.txt
```

Steward reads credentials from `/run/secrets/` at runtime. Benefits:
- Keys never traverse the container filesystem (tmpfs mount, not copied).
- No access to `~/.ssh/` required — users generate a dedicated deploy key.
- Docker restricts secret file visibility to the specific service.

---

#### 6.2.2 Per-repo credential references (credential indirection)

**Industry standard:** ArgoCD repository credentials stored as named secrets, referenced by
URL pattern. Flux CD `GitRepository` objects reference a `secretRef`.

**Proposed solution:** Add a `credentials` section to the steward configuration (separate
from app manifests) that maps URL patterns to credential sources:

```yaml
# credentials.yml (mounted into container, NOT committed to control repo)
credentials:
  - pattern: "github.com/org/*"
    type: ssh
    key_file: /run/secrets/github_deploy_key
  - pattern: "gitlab.com/team/*"
    type: token
    token_file: /run/secrets/gitlab_token
  - pattern: "*"
    type: ssh
    key_file: /run/secrets/default_key
```

Benefits:
- App manifests contain only plain repo URLs (no embedded tokens).
- Credentials never appear in the control repo.
- Per-repo or per-org scoping enables least-privilege.
- Credential rotation requires only replacing the secret file and restarting steward.

---

#### 6.2.3 SSH agent socket forwarding (opt-in alternative)

**Industry standard:** Forward the host SSH agent socket into the container via bind-mount.

```yaml
volumes:
  - ${SSH_AUTH_SOCK}:/run/ssh-agent:ro
environment:
  SSH_AUTH_SOCK: /run/ssh-agent
```

Benefits:
- Private keys never leave the host's memory (agent holds them).
- No keys on the container filesystem at all.
- Key rotation is transparent (reload agent on host).

Drawbacks:
- Requires the host to run `ssh-agent` (not always the case for headless servers).
- If the container is compromised, the attacker can use the agent to authenticate (but cannot
  extract the keys themselves — mitigated by agent key confirmation or timeout).
- Less suitable for unattended/headless homelab nodes without user sessions.

---

#### 6.2.4 Git credential helper integration

**Industry standard:** Git's built-in credential helper system (`git credential fill`).

**Proposed solution:** Instead of embedding tokens in URLs, configure git inside the container
to use a credential helper that reads from a secret file:

```
[credential "https://github.com"]
    helper = !f() { echo "username=oauth2"; echo "password=$(cat /run/secrets/github_token)"; }; f
```

Benefits:
- Tokens are never part of the URL (no risk of log/metric leakage).
- Works with any HTTPS git remote without URL modification.
- Compatible with the Docker secrets approach (6.2.1).

---

### 6.3 Recommendations (based on established standards)

| # | Recommendation | Precedent |
|---|---|---|
| 1 | **Use dedicated deploy keys** — never mount `~/.ssh/`. Generate a purpose-specific ed25519 key per repo or org. | GitHub Deploy Keys, GitLab Deploy Keys, Flux CD guidance |
| 2 | **Store credentials as Docker Compose secrets** — mount via `/run/secrets/`, not environment variables or bind-mounted directories. | Docker security best practices, 12-factor app secrets guidance |
| 3 | **Never embed tokens in URLs committed to git** — use credential indirection (file reference or credential helper). | ArgoCD repository credentials, OWASP secret management |
| 4 | **Support per-repo credential scoping** — map URL patterns to credential sources for least-privilege access. | ArgoCD credential templates, Flux CD `secretRef` |
| 5 | **Strip credentials from all output paths** — extend `strip_url_credentials()` coverage to git command error messages and debug logs. | General security hygiene |
| 6 | **Document a migration path** — the current `~/.ssh` bind-mount approach must remain supported (deprecated) for backward compatibility, but new documentation should guide users to the secure path. | Flux CD v1→v2 migration |

---

### 6.4 Decisions

| # | Decision | Resolution |
|---|---|---|
| 1 | **Primary credential mechanism** | Docker secrets only — no SSH agent forwarding. |
| 2 | **Credential config format** | Separate `credentials.yml` file, mounted from host filesystem (mirrors `.env`). |
| 3 | **Per-app vs global credentials** | Per-URL-pattern matching in `credentials.yml`; full glob patterns; path components warn and are stripped to hostname for SSH matching. |
| 4 | **Deprecation timeline for `~/.ssh` bind-mount** | ~~Deprecated now with explicit warning; removed in next major version.~~ **Removed in 0.3.0** — container exits with error if bind-mount detected. |
| 5 | **Scope boundary with secrets management** | Credential access (git keys/tokens) is in scope; app secrets remain out of scope. |
| 6 | **Token leakage in error paths** | Resolved in Phase 1 — `strip_url_credentials()` applied to all log paths. |

---

### 6.5 Implementation phases

**Phase 1 (low effort, high impact):** ✓ Complete

- ✓ Audit and sanitize all log/error paths for credential leakage — `strip_url_credentials()` now applied to all `GitCommandError` messages (`fetch_ref`, `apply_ref`, `reconcile_app`, `run`) and the clone log line in `ensure_repo`. Startup banner in `entrypoint.sh` also masks embedded credentials.
- ✓ Document "dedicated deploy key" as the recommended approach (over mounting `~/.ssh/`) — README updated.
- ✓ Support reading SSH key from `/run/secrets/ssh_key` as an alternative to bind-mount — `entrypoint.sh` reads Docker secret, writes to `~/.ssh/id_ed25519` with correct permissions and auto-generated SSH config.
- HTTPS token support (`/run/secrets/control_repo_token`) is superseded by the stricter decision to enforce SSH-only URLs (`validate_repo_url()` rejects all HTTPS URLs at parse time). No HTTPS token path is needed.

**Phase 2 (medium effort):** ✓ Complete

- ✓ `credentials.yml` for per-repo credential mapping — `parse_credentials_file()` and `generate_ssh_config()` in `steward.py`; full glob patterns supported; hostname extracted for SSH `Host` entries; path-component patterns warn at parse time.
- ✓ `GIT_SSH_COMMAND` per-repo configuration — replaced by the `credentials.yml` + SSH config approach; each host gets its own `Host` block with `IdentityFile` and `IdentitiesOnly yes`.
- ✓ SSH agent socket forwarding — explicitly out of scope per decision (Docker secrets only).
- ✓ Deprecation warnings for legacy `~/.ssh-host` bind-mount — superseded by 0.3.0 breaking removal (see below).
- ✓ Priority chain in `entrypoint.sh`: (1) `credentials.yml`, (2) `/run/secrets/ssh_key` single-key. Legacy `.ssh-host` branch removed in 0.3.0.
- ✓ `STEWARD_CREDENTIALS_FILE` env var (default `/app/credentials.yml`) controls the credentials config path.

**0.3.0 breaking change:** ✓ Complete

- ✓ Removed `/root/.ssh-host` bind-mount support entirely from `entrypoint.sh`. Container now exits with an error (exit code 1) and a link to the migration guide if `/root/.ssh-host` directory is detected.
- ✓ README updated: migration section reflects hard error; old multi-key bind-mount docs replaced with `credentials.yml` pointer.

**Phase 3 (longer term):****
- Consider integration hooks for external secret managers (Vault agent sidecar, 1Password CLI).
- Credential rotation detection (watch secret files for changes, re-init git config).

**Complexity:** Phase 1 is Low, Phase 2 is Medium, Phase 3 is Medium-High.

**Depends on:** None (can be implemented independently of other goals).

---

## Goal 7 — Fix parse-error visibility in reconcile metrics

### Problem

When a manifest fails to parse (e.g. bad URL, missing required field, YAML syntax error), steward silently drops the app. This causes three cascading problems that make the failure nearly invisible:

| Bug | Observable symptom |
|---|---|
| Parse-error apps never added to `results` dict | Summary log shows `total=2 ok=2 failed=0` even though 3 manifests exist — steward.yml silently absent |
| `run_result` computed only from `results` | `steward_reconcile_total{result="partial_failure"}` never fires when only parse errors occur; run records as `success` |
| Stale SQLite state persists unchanged | Metrics show old `Synced` status and old timestamp for the failing app; "Repeated reconcile failures" never fires; "App not reconciled" fires only after 5+ minutes due to timestamp aging |

**Confirmed from production log:**

```
2026-05-25T10:45:02 ERROR [steward] Skipping invalid manifest steward.yml: Invalid manifest … repo: only SSH URLs are supported …
2026-05-25T10:45:08 INFO  [steward] Reconciliation complete | total=2 disabled=0 ok=2 failed=0
```

Three manifests exist on disk; one failed to parse; log and metrics show only two.

**Operational note:** The specific error message in the production log ("only SSH URLs are supported") comes from old deployed code predating the HTTPS support commit already in the codebase. That error resolves on next rebuild/deploy. These fixes improve observability for any future parse errors.

---

### Design decisions

- **Parse-error apps are treated as `failed` apps** throughout the reconcile pipeline — they get a `results` entry, app state written to SQLite, and `reconcile_total.failed` incremented every cycle.
- **`sync_status = Unknown`, `health_status = Unknown`** — we literally cannot know the state when the manifest is unparseable; neither `Degraded` nor `OutOfSync` is correct.
- **`last_reconcile_timestamp` updated every cycle** — prevents "App not reconciled" from falsely firing for parse-error apps, and lets "Repeated reconcile failures" carry the signal instead.
- **`parse_errors=N` always included in summary log line** (option A) — consistent, parseable, makes `parse_errors=0` explicitly visible.
- **App name fallback = `manifest_file.stem`** — consistent with how steward normally derives app names from filenames (e.g. `steward.yml` → `steward`).
- **New global alert on `steward_manifest_parse_errors_total`** — fires on the very first parse error, before "Repeated reconcile failures" accumulates 3 cycles. Complementary, not redundant.
- **Not in scope:** removing stale SQLite rows when a manifest file is intentionally deleted — different problem with different tradeoffs.

---

### Dashboard & alert impact analysis

#### Existing alerts

| Alert | Impact |
|---|---|
| Compose apply failed (`steward_app_sync_total{result="failed"}`) | No impact — parse-error apps never reach `run_compose` |
| **Repeated reconcile failures** (`steward_app_reconcile_total{result="failed"}[15m] > 2`) | ✅ Will now fire — this is the intended signal for parse-error apps |
| Node not reporting | No impact |
| **App not reconciled** (`time() - steward_app_last_reconcile_timestamp_seconds > 300`) | ✅ Fixed — no longer falsely fires for parse-error apps once timestamp is updated each cycle |
| App health degraded | No impact — Unknown is written, not Degraded |
| App remains out of sync | No impact — Unknown is written, not OutOfSync |
| Control repo sync failed | No impact |

#### Dashboard panels

| Panel | Impact |
|---|---|
| Active Apps (`count(steward_app_info{enabled="true"})`) | Parse-error apps appear in count (same as today with stale state — no regression) |
| Apply Failures (1h) | No impact |
| App Status table — Reconcile Failures (1h) column | ✅ Now shows incrementing count for parse-error app instead of blank/stale |
| App Status table — Last Reconcile (s ago) column | ✅ Shows fresh timestamp instead of growing stale age |

---

### Implementation plan

**Phase 1 — Richer return type from `load_node_manifests`** — `steward.py` ~line 1099

Change return type from `(list[AppManifest], int)` to `(list[AppManifest], list[tuple[str, str, str]])`.  
Each error tuple: `(filename, app_name, error_msg)` where `app_name` is the `name` key extracted from raw YAML via `yaml.safe_load()`, falling back to `manifest_file.stem` if parsing fails or the key is absent.  
Update docstring.

**Phase 2 — Write error state + add to `results`** — `steward.py` `reconcile()` ~lines 1459–1475

At the call site, rename `parse_errors` → `parse_error_entries`; update `_inc` to use `len(parse_error_entries)`.

After `results = {}` is initialized, add a loop over `parse_error_entries` that for each entry:
- Writes `state["apps"][app_name]` with `enabled=True`, `sync_status=Unknown`, `health_status=Unknown`, `last_reconcile_timestamp=time.time()`
- Calls `_inc(app_state, "reconcile_total", "failed")`
- Sets `results[app_name] = "failed"`

Cascade effects (no other code changes needed):
- `total` and `failed` counts correct in summary log
- `run_result = "partial_failure" if failed else "success"` naturally becomes `partial_failure`
- Per-app `reconcile_total{result="failed"}` increments → "Repeated reconcile failures" fires after 3 cycles
- `last_reconcile_timestamp` stays fresh → "App not reconciled" stops falsely firing

**Phase 3 — `parse_errors=N` always in summary log** — `steward.py` ~line 1511

Change format string to `"Reconciliation complete | total=%d disabled=%d ok=%d failed=%d parse_errors=%d"` and add `len(parse_error_entries)` as final arg.

**Phase 4 — Tests** — `tests/test_steward.py`

- Fix two broken monkeypatches: lines 1245 and 1279 — change `([..., 0)` → `([..., [])`
- Add test: parse-error entry → `results[app_name] == "failed"` and `state["apps"][name]["sync_status"] == "Unknown"`
- Add test: `run_result` is `partial_failure` when `load_node_manifests` returns non-empty error list

**Phase 5 — New alert in README_prometheus.md**

Add `steward-manifest-parse-error` alert (in both provisioning YAML and Option B table):
- Expression: `increase(steward_parse_errors_total[5m]) > 0`
- Severity: `warning`
- `for: 0s`
- Description: fires on the first parse error, before "Repeated reconcile failures" accumulates

Also add `steward_manifest_parse_errors_total` to the Grafana alert summary table in `README.md`.

---

### Status

- [x] Phase 1 — Richer return type
- [x] Phase 2 — Write error state + results entry
- [x] Phase 3 — parse_errors in summary log
- [x] Phase 4 — Tests
- [x] Phase 5 — New alert in README_prometheus.md + README.md

---

## Goal 8 — New-node setup UX

### Problem

Getting steward running on a new node requires a user to:
- Read the full quick-start section across multiple subsections
- Manually create 2–3 files (`.env`, `docker-compose.override.yml`, optionally `credentials.yml`) from scattered examples in the README
- Make a branching decision (single-key vs multi-key) without clear guidance on which is right for their case
- Know that `AGENT_IMAGE` in `.env` is a testing escape hatch, not the normal update mechanism

This is more friction than necessary for what should be a 5-minute operation.

### Design decisions

- **Single deploy key per node is the default and recommended path** — one key with access to all repos on that node; principle of least privilege.
- **Multi-key (`credentials.yml`) is supported** but clearly a secondary option for users with keys across multiple git hosts.
- **`AGENT_IMAGE` is absent from all user-facing templates** — it is a testing escape hatch for image overrides during development, not a user concern; Dependabot manages the version in `docker-compose.yml`.
- **Templates are non-opinionated about paths** — example files use placeholder paths (`/home/you/.ssh/…`) so users fill in their own; no baked-in assumptions about where keys live.
- **Templates are committed; generated files are not** — `.env.example` and `docker-compose.override.yml.example` live in the repo as reference; `.env` and `docker-compose.override.yml` are gitignored on the deployment host.

---

### Phase 1 — Templates and documentation (first)

**Scope:** No scripts. Two new template files and a streamlined README quick-start. Goal: a user can get from zero to `docker compose up -d` by following a single linear path.

#### 1a. `.env.example`

New file at repo root. All user-facing variables with inline comments. Required values clearly marked; optional values commented out with defaults shown. Excludes `AGENT_IMAGE`.

```
# Required
CONTROL_REPO_URL=git@github.com:you/homelab-gitops.git
GITOPS_NODE_NAME=node1.lan
STEWARD_DATA_DIR=/opt/steward-data

# Recommended — set to your host user to avoid root-owned files in STEWARD_DATA_DIR
# STEWARD_UID=1000
# STEWARD_GID=1000

# Optional
# CONTROL_REPO_BRANCH=main
# LOGLEVEL=INFO
# METRICS_PORT=
```

#### 1b. `docker-compose.override.yml.example`

New file at repo root. Single-key variant uncommented (default); multi-key variant commented out below with a clear separator. User copies the file, removes the `.example` suffix, and edits paths.

Single-key section:
```yaml
services:
  steward:
    secrets:
      - ssh_key
      - ssh_known_hosts   # optional

secrets:
  ssh_key:
    file: /home/you/.ssh/steward_deploy_key
  ssh_known_hosts:
    file: /home/you/.ssh/known_hosts   # optional
```

Multi-key section (commented out):
```yaml
# --- Multi-key setup (multiple git hosts) ---
# Uncomment the block below and comment out the single-key block above.
# Also create /etc/steward/credentials.yml — see README §5.
#
# services:
#   steward:
#     volumes:
#       - /etc/steward/credentials.yml:/app/credentials.yml:ro
#     secrets:
#       - github_key
#       - gitlab_key
#       - ssh_known_hosts   # optional
#
# secrets:
#   github_key:
#     file: /home/you/.ssh/github_deploy_key
#   gitlab_key:
#     file: /home/you/.ssh/gitlab_deploy_key
#   ssh_known_hosts:
#     file: /home/you/.ssh/known_hosts
```

#### 1c. README.md quick-start streamline

Replace current steps 3–5 (scattered .env instructions + single-key + multi-key subsections) with a single linear flow:

1. `cp .env.example .env` → fill in 3 required values
2. `echo "STEWARD_UID=$(id -u)" >> .env && echo "STEWARD_GID=$(id -g)" >> .env`
3. Generate deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/steward_deploy_key -N "" -C "steward@$(hostname)"`
4. Add public key to GitHub/GitLab as deploy key
5. `cp docker-compose.override.yml.example docker-compose.override.yml` → update key path
6. `docker compose up -d`

Keep multi-key and credentials.yml content but demote it to a separate "Advanced: multiple git hosts" subsection rather than an equal-weight option alongside single-key.

---

### Phase 2 — Interactive setup script (later)

**Scope:** `setup.sh` — a POSIX sh script that automates Phase 1 steps.

- Prompts for `CONTROL_REPO_URL`, `GITOPS_NODE_NAME`, `STEWARD_DATA_DIR`
- Auto-detects `STEWARD_UID`/`STEWARD_GID`
- Writes `.env` (aborts if already exists)
- Asks: "Single deploy key (recommended) or multiple keys? [1/2]"
- **Single-key path:** offers to generate key; writes `docker-compose.override.yml`; prints public key; waits for confirmation
- **Multi-key path:** prompts for N host/key pairs; writes `credentials.yml` and `docker-compose.override.yml`
- Asks: "Start steward now? [Y/n]"; if yes: `docker compose up -d`
- POSIX sh only, no external dependencies beyond `ssh-keygen` and `docker`
- Non-destructive: never overwrites existing `.env` or `docker-compose.override.yml`

Depends on: Phase 1 complete (script uses same templates as reference).

---

### Phase 3 — Setup diagnostic script (`scripts/doctor.sh`)

**Scope:** A standalone bash script that validates a node's steward setup **both on the host
and inside the running container**. The validation logic stays OUT of `steward.py` (the runtime
stays lean); this is an external operator tool. It exists to catch the class of misconfiguration
that otherwise costs hours of debugging — a wrong host in `CONTROL_REPO_URL` (e.g. `github.com`
where `gitlab.com` was meant), an empty/mismatched `known_hosts`, or a missing deploy key — and
to surface the failure in one clear line instead of a cascade of misleading SSH errors.

**Design decisions:**

- bash, located at `scripts/doctor.sh`, executable.
- **Run from the deployment directory** — the user `cd`s into the steward deployment dir first;
  the script reads `.env`, `credentials.yml`, `docker-compose.override.yml` from CWD. No `--dir`
  flag.
- One invocation from the host: it runs host checks, then auto-detects the running container and
  `docker exec`s into it for the in-container checks.
- **Run all checks, then print a pass / warn / fail summary; exit non-zero if any check FAILed.**
- Performs a **live `git ls-remote`** inside the container against `CONTROL_REPO_URL` — this
  reproduces the real clone path. (Note: `ssh -T git@host` is NOT a valid test — deploy keys are
  repo-scoped and always return `Permission denied (publickey)` even on a working node.)

**Gotchas the script must encode (learned the hard way):**

- `docker exec` defaults to root, so `~` resolves to `/root`; but steward runs as a non-root user
  with `HOME=/home/steward`. The script must detect the effective HOME, not assume `~`.
- With `credentials.yml`, the known_hosts file lives at the secret path referenced by
  `UserKnownHostsFile` in the generated `~/.ssh/config` — NOT at `~/.ssh/known_hosts`.
- Referencing a `known_hosts` file forces `StrictHostKeyChecking yes`; if it is empty or missing
  the control-repo host, every clone fails with `Host key verification failed`.
- A wrong host in `CONTROL_REPO_URL` surfaces as `Permission denied (publickey)` only *after*
  host-key trust passes — so the script echoes the resolved host explicitly.

**Checks:**

*Phase A — host:*
1. `docker` and `docker compose` v2 present.
2. `.env` exists; `CONTROL_REPO_URL` set and non-empty; warn if still the example value.
3. URL format is SSH/HTTPS; FAIL on embedded credentials.
4. Extract and **echo the host** from `CONTROL_REPO_URL` (makes github-vs-gitlab typos obvious).
5. `STEWARD_DATA_DIR` exists/creatable; `STEWARD_UID`/`STEWARD_GID` set (warn if root).
6. If `credentials.yml` is referenced: it parses and each `key_file` exists on the host.
7. If a known_hosts file is referenced: it exists AND contains an entry for the control-repo host.

*Phase B — container (skip + warn if not running):*
8. Container `steward` is running.
9. Detect effective `HOME` inside the container → derive the SSH config path.
10. Show the generated SSH config: `StrictHostKeyChecking` + `UserKnownHostsFile`/`IdentityFile`.
11. Resolve the known_hosts path SSH actually uses; verify it contains the control-repo host key.
12. Verify the identity key file exists and is non-empty in the container.
13. **Live test:** `docker exec … git ls-remote <CONTROL_REPO_URL>` via the container's SSH
    config; PASS if refs return, FAIL with captured stderr.
14. Compare the container's `CONTROL_REPO_URL` env to `.env` (catches drift).

*Summary:* `N passed / N warnings / N failed`; exit 1 if any failed.

**Companion README changes:**

- Add a "Validate your setup" step to the quick start pointing to `scripts/doctor.sh`.
- Fix the existing Troubleshooting diagnose commands — they use `~/.ssh/config` and
  `~/.ssh/known_hosts`, which are wrong under `docker exec` (root HOME) and for the credentials.yml
  path (known_hosts at the secret path). Use `/home/steward/.ssh/config` (with `/root` fallback)
  and the resolved `UserKnownHostsFile` path.
- Add a `Permission denied (publickey)` Troubleshooting entry (causes: wrong host/typo in
  `CONTROL_REPO_URL`; deploy key not registered with write access; the `ssh -T` false-negative —
  use `git ls-remote <repo>` instead).

**Verification:** `bash -n scripts/doctor.sh` and `shellcheck`; run on a known-good node (all pass)
and against a deliberately broken URL (clear FAIL at the `git ls-remote` step).

**Complexity:** Low — read-only diagnostic; no `steward.py` runtime changes.

---

### Status

- [x] Phase 1a — `.env.example`
- [x] Phase 1b — `docker-compose.override.yml.example`
- [x] Phase 1c — README quick-start streamline
- [ ] Phase 2 — `setup.sh`
- [x] Phase 3 — `scripts/doctor.sh` setup diagnostic + README troubleshooting fixes

---

## Bug: phantom third container after a few days

**Status: fixed**

After a clean `docker compose up -d` two containers run (steward + metrics server side-car).
After several days `docker ps` shows three containers. Analysis identified three candidate
bugs; all three have been addressed.

### Candidate Bug 1 — helper container stuck because inner compose has no timeout (fixed)

`spawn_compose_helper` spawns:

```
docker run --rm -d ... sh -c "sleep 5 && docker compose up -d --remove-orphans --pull always"
```

`--pull always` contacts the registry on every self-update. A transient GHCR outage or
throttle causes `docker pull` to retry indefinitely with no deadline. The inner `sh -c` never
exits; `--rm` never fires; the helper container lives forever.

By contrast `run_compose()` uses `subprocess.run(..., timeout=300)`, so the direct path is
protected. The helper path had no equivalent guard.

**Fix applied:** prefixed the inner command with `timeout 300`:

```bash
sh -c "sleep 5 && timeout 300 docker compose up -d --remove-orphans --pull always"
```

Also fixed in the same change: `--env-file` was being appended after the subcommand in
`spawn_compose_helper`, `run_compose`, `_load_expected_services`, and
`_load_compose_services_status`. Docker Compose requires `--env-file` before the subcommand.
This caused `docker compose config --services` to fail with `unknown flag: --env-file`,
producing `expected_services_unavailable` on every reconcile for apps with an env file.

### Candidate Bug 2 — drift self-heal bypasses `_is_self_update` check (fixed)

In `reconcile_app`, the live-drift self-heal path (git SYNCED but service not running) was
calling `run_compose(app, stack_path)` directly, bypassing `sync_app` which is the only place
`_is_self_update` was tested. When steward's own service drifted, it called
`docker compose up -d` on itself, which killed the running process; `restart: unless-stopped`
brought it back, then the next reconcile cycle saw drift again → kill loop.

**Fix applied:** the self-heal path now uses `spawn_compose_helper` when `_is_self_update(app)`
is true, matching the behaviour of `sync_app`.

### Candidate Bug 3 — no lock between concurrent reconcile processes

Cron fires every minute. If a reconcile run takes > 60 s (many apps, slow fetches), two
processes run concurrently and both can reach `spawn_compose_helper` before either has
updated `local_sha`. Both see `OUT_OF_SYNC` and both spawn helpers simultaneously.

Combined with Bug 1 (now fixed), one of those helpers could get permanently stuck.

**Status:** Bug 1 fix (timeout 300) means stuck helpers will now self-terminate. The root
concurrent-spawn issue is not fixed but its worst consequence (permanent stuck container)
is now bounded. A proper flock guard is deferred.

---

## Non-goals

These ArgoCD concepts are deliberately out of scope for steward's design. They are listed here
to prevent scope creep and to document the reasoning.

- **Rollback** — git revert is the rollback mechanism; steward tracks forward only. Adding a
  `steward rollback` command would require storing previous compose state, which is fragile.
  The correct answer is always "revert the manifest commit in Git."
- **Sync waves / ordering** — compose files declare dependencies via `depends_on`; steward
  delegates inter-service ordering to compose entirely.
- **Multi-node coordination** — one steward per node is the intended model. Fleet-wide views
  are provided by the Prometheus + Grafana layer, not by steward itself.
- **UI / web dashboard** — Grafana dashboards cover the observability need. A TUI (`steward
  status`) is acceptable; a web UI is not in scope.
- **Application secrets management** — env files on the node filesystem are the secret boundary
  for application-level secrets (database passwords, API keys for apps). Integration with Vault,
  SOPS, or similar for *app secrets* is out of scope; steward is not a secrets operator.
  Note: *git credential handling* (SSH keys, tokens for repo access) is in scope — see Goal 6.


---

## Fix: Zombie process leak (PID 1 reaping failure)

### Diagnosis

**Symptoms:** `error: cannot fork() for ssh: Resource temporarily unavailable` on all git
operations; status writeback also fails with `[Errno 11]`.

**Evidence gathered:**
- `ulimit -u` inside container → `unlimited` (not a container nproc limit)
- `ps -u 1000 --no-headers | wc -l` on host → **9067 processes** for UID 1000
- `ps -u 1000 -o stat,comm` breakdown: **9036 `Zs git`**, 21 `Z ssh`, ~9060 zombies total
- Reconcile duration graph is healthy (5–10 s baseline); confirms NOT overlapping runs

**Root cause:** Two compounding defects:

1. **PID 1 is busybox `crond`** (`exec crond -f -l 6` in entrypoint) — crond never calls
   `wait()`, so orphaned children reparent to it and become permanent zombies.
2. **GitPython keeps persistent `git cat-file --batch` and `ssh` children alive** per `Repo`
   object. When each per-minute reconcile process exits, those children are orphaned and
   reparent to PID 1 (crond). With no reaping init, they accumulate at ~1440/day, reaching
   9036 over ~6–8 days until the host PID space is exhausted.

Secondary: `control_repo.close()` (steward.py) was not in a `try/finally`, risking a close
skip if status writeback raised — reduces zombie rate when fixed.

### Fix

**Files changed:**

| File | Change |
|---|---|
| `Dockerfile` | Added `tini=~0.19` to `apk add` |
| `entrypoint.sh` | Changed `exec crond -f -l 6` → `exec tini -- crond -f -l 6` |
| `docker-compose.yml` | Added `init: true` (defence-in-depth) |
| `steward.py` | Wrapped `control_repo.close()` in `try/except` |

tini becomes PID 1 and reaps every orphaned zombie in the namespace automatically (no `-s`
subreaper flag needed when tini IS PID 1). `init: true` in compose is redundant with the
image fix but covers bootstrap/local deploys and documents intent.

**Cleanup:** Deploying the new container image recreates the container, clearing the
accumulated 9036 zombies via a fresh PID namespace.

**Status: fixed** — implemented and pushed to main 2026-06-07

### Follow-up: tini PID-1 warning regression

**Status: planned (not yet implemented)**

The zombie fix added BOTH `init: true` in `docker-compose.yml` AND `exec tini -- crond` in
`entrypoint.sh`. With `init: true`, Docker injects `docker-init` as PID 1, so the entrypoint's
own `tini` no longer runs as PID 1 — it starts as a non-PID-1 process and logs on every boot:

```
[WARN tini (7)] Tini is not running as PID 1 and isn't registered as a child subreaper.
```

Reaping still works (docker-init at PID 1 reaps the orphaned zombies), so this is harmless but
noisy and confusing. The two init layers are redundant.

**Fix — Option A (chosen): single init via compose, drop the in-image tini.**

| File | Change |
|---|---|
| `entrypoint.sh` | `exec tini -- crond -f -l 6` → `exec crond -f -l 6`; update the comment block (reaping is provided by compose `init: true`, not tini) |
| `Dockerfile` | Remove `tini=~0.19` from the `apk add` list (mind the `&& \` chaining) |
| `docker-compose.yml` | Update the `init: true` inline comment — drop "alongside tini in the image" |

Keep `init: true` as the single reaping init. (Trade-off: local `docker run` without `--init`
would lose reaping, but steward is always deployed via compose, which sets `init: true`.)

**Verification:** container starts with no `[WARN tini …]` line; under load, orphaned git/ssh
children are still reaped (docker-init at PID 1).

---

## Decision: drop git status writeback (return to GitOps principles)

**Status: decided 2026-06-10 — implementation pending.**

### Trigger

On media-1 (running fine for weeks), after adding a second node (agent-1) to the control repo,
status writeback began failing every cycle:

```
ERROR [steward] Status writeback failed: Cmd('git') failed due to: exit code(1)
  cmdline: git push origin main
  stderr: ! [rejected]  main -> main (fetch first) … Updates were rejected because the
          remote contains work that you do not have locally.
```

and the **next** reconcile could no longer pull:

```
ERROR [steward] git pull/checkout failed: exit code(128)
  hint: You have divergent branches and need to specify how to reconcile them.
```

### Root cause

1. `_write_status_snapshot` commits `nodes/<host>/status.json` locally, **then** pushes. When
   another node pushed in between, the push is rejected — but the local commit stays, so the
   control working copy diverges from `origin/main`.
2. `apply_ref` pulls the control repo with a bare `git pull origin main` (no merge strategy), so
   git ≥ 2.27 refuses the divergent branches with `exit 128`. The node is then wedged: it can
   neither advance the control repo nor write status.

Each node writes a *different* file, so there is never a content conflict — only a
fast-forward conflict. The real defect is architectural: **the reconciler writes to the same
ref it reads desired state from.**

### Why it was nearly invisible in Grafana (the worrying part)

- `_status_writeback` is a **pseudo-app** — only a key in the in-memory `results` dict, never
  written to `state["apps"]`, so `steward_app_reconcile_total{app="_status_writeback"}` is never
  emitted and "Repeated reconcile failures" cannot fire.
- Control-repo-sync failure bumps `steward_control_repo_sync_total{result="failed"}` but does
  **not** feed `run_result`, and there is **no alert** on that counter.
- Freshness alerts stay green because steward keeps running each minute and updates
  `last_timestamp`. The node looks healthy.

### Decision

Return to GitOps principles: **remove the writeback entirely** (Option 1 — most GitOps-pure).
Observed state lives only in Prometheus + SQLite; git holds desired state only. This also
eliminates the push race and the divergent-pull deadlock at the root — if steward never commits
to the control repo, `origin/main` only moves forward relative to local, so `git pull` always
fast-forwards.

### Removal scope (implementation pending)

**Code (`steward.py`):**
- Remove `_build_status_snapshot` and `_write_status_snapshot`.
- Remove `_ts_to_iso` (only the snapshot used it). Keep `_now_iso` (used by operation history
  and notifications).
- In `reconcile()`, remove the `_write_status_snapshot(...)` call, `status_write_ok`, the
  `_status_writeback` pseudo-app `results` entry, and the writeback `partial_failure` branch.
  Keep `control_repo.close()`.

**Observability (close the remaining blind spots):**
- Add a Grafana alert on `increase(steward_control_repo_sync_total{result="failed"}[15m]) > 0`.
- Add a Grafana alert on `increase(steward_reconcile_total{result="partial_failure"}[15m]) > 0`.

**Docs:**
- README: control-repo deploy key drops to **read-only** (was read/write for writeback) — a
  least-privilege improvement; call it out.
- README: remove the "write observed state snapshot to `nodes/<hostname>/status.json`" step from
  the reconciler-flow diagram.
- README: state explicitly that observed state is exposed via Prometheus + SQLite, not written
  back to git (reinforces the item 2.2 write-path contract).
- README: add the two new alerts to the Grafana alerts table.
- `sequence.md`: amend the Phase 7 note — 2.1 writeback removed 2026-06-10; 2.2 (dry-run / oob
  contract) stays.

**Tests:**
- Add a guard test: a reconcile performs **no** commit/push on the control repo (HEAD stays at
  `origin` HEAD). (No existing writeback tests to delete.)

### Decided follow-ups

1. **Delete** the existing `nodes/<host>/status.json` files from the control repo — they are now
   stale observed-state artifacts. This is a **one-time manual cleanup commit by a human** (steward
   no longer touches the control repo). Also scan the control repo / homelab-gitops for any
   consumer that reads `status.json` and adjust it so nothing breaks.
2. Optional control-repo `reset --hard origin/<branch>` self-heal hardening: **deferred** —
   fast-forward is guaranteed once writeback is gone; revisit only if manual drift on a node bites.

### Operational recovery (manual, one-off)

media-1 is currently wedged on a divergent local control-repo commit. Recover with
`git -C /git/control reset --hard origin/main` (or redeploy the container). This cannot recur
once the writeback removal ships.

### Verification

- `uv run ruff check steward.py metrics_server.py tests/`
- `uv run pytest tests/ -v --tb=short` (including the new no-push guard test)
- Manual: after a reconcile, no `status.json` commit appears; control repo HEAD == origin HEAD;
  metrics still expose sync/health status.
