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
Write the current status for all apps to `nodes/<hostname>/status.json` in the control repo
after each cycle (see item 2.1).

**Open questions:** resolved.
- What is the correct `OutOfSync` definition when `sync_policy: manual` is set (item 1.3)?
  Should steward report `OutOfSync` but never act, or use a distinct `Pending` status?
- When a manifest is newly added (first-ever deploy), should the initial status be `Unknown`
  or `OutOfSync`? ArgoCD uses `Unknown` until the first comparison completes.
- Should `Disabled` be a sync status or a separate field in the state? Keeping it separate
  avoids conflating operational state with sync state.

**Complexity:** Medium — requires items 3.1 (SQLite state) and 2.1 (status writeback) as
foundations, but the status logic itself is straightforward once those exist.

**Depends on:** 2.1, 3.1

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

**Open questions:**
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

**Open questions:**
- Should `manual` mode also suppress the self-heal trigger (item 1.4)? i.e. if a container
  crashes, does `sync_policy: manual` mean steward never heals it either? Likely yes — the
  operator has opted out of automatic changes. But this should be an explicit decision.
- Is `sync_policy` a manifest-level field (per app) or a node-level env var (per node)?
  Per-app is more flexible; per-node is simpler. Could be both with per-app overriding the
  node default.
- What notification behaviour should accompany `OutOfSync` in manual mode? Should it trigger
  the failure notification (item 3.3) or a distinct `drift_detected` notification type?

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

**Open questions:**
- What is the authoritative list of "expected services"? Options: (a) parse the compose file
  and enumerate all services with `restart` policies other than `no`, (b) run
  `docker compose config --services` and treat all services as expected, (c) use the last
  successful `compose ps` output as the baseline. Option (b) is simplest and most correct.
- How do we handle intentionally stopped services? If an operator runs `docker stop arr_sonarr`
  for maintenance, steward will immediately restart it. Should there be a `steward suspend <app>`
  mechanism, or is `enabled: false` + commit the correct GitOps answer?
- Should self-heal be gated by `sync_policy`? (See open question in item 1.3.)
- What is the restart-loop detection threshold? If a service restarts 3 times in 5 minutes,
  should steward stop trying to heal it and report `Degraded` instead? This prevents a
  storm of `compose up` calls against a broken container.

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

**Open questions:**
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

**Complexity:** Medium — SQLite is in the Python stdlib (`sqlite3`), so no new dependency.
The schema is simple. The main work is migrating away from the existing JSON state file and
wiring the insert into the reconcile loop.

---

## Goal 2 — GitOps idiom: redesign non-idiomatic features

These items correct behaviours that work today but violate GitOps principles or introduce
subtle correctness problems that will compound as steward matures.

---

### 2.1 Observed state writeback to control repo

**GitOps principle:** Git is the single source of truth for both desired state _and_ observed
state. A pure GitOps system writes its observed state back to the same repo so the repo is a
complete picture of the fleet at any point in time.

**Problem:** The control repo currently holds only desired state (app manifests). There is no
machine-written record of what is actually running on each node. An operator looking at the
control repo cannot answer "is node1 actually running arr v1.2.3 right now?" without SSHing to
the node or checking Grafana.

**Proposed solution:** After each reconcile cycle, steward writes a
`nodes/<hostname>/status.json` file back to the control repo via a git commit + push. This file
contains the current sync and health status for every app on the node:

```json
{
  "node": "node1.lan",
  "updated_at": "2026-05-19T08:00:05Z",
  "apps": {
    "arr": {
      "sync_status": "Synced",
      "health_status": "Healthy",
      "deployed_sha": "def456",
      "remote_sha": "def456",
      "last_synced_at": "2026-05-19T07:45:01Z"
    },
    "prometheus-node-exporter": {
      "sync_status": "OutOfSync",
      "health_status": "Healthy",
      "deployed_sha": "abc123",
      "remote_sha": "ghi789",
      "last_synced_at": "2026-05-18T14:00:01Z"
    }
  }
}
```

The full operation history stays in SQLite locally (item 1.5). The status file is the lightweight
"observed state snapshot" — the equivalent of what ArgoCD writes to the Application CRD's
`.status` subresource.

**Open questions:**
- Write access to the control repo requires a token with push rights. This is a broader
  permission than the current read-only token. Should the writeback use a separate deploy key
  scoped to only the `nodes/<hostname>/` path, or is a single read/write token acceptable?
- What is the commit frequency? Writing after every reconcile cycle (every minute) will generate
  significant commit noise in the control repo. Options: (a) only write when status changes,
  (b) write at most once per N minutes (e.g. 5), (c) write to a dedicated `status` branch so
  `main` stays clean. Option (a) is most GitOps-correct and generates the least noise.
- Should the `status.json` file be committed by steward directly (using gitpython) or via the
  GitHub/GitLab API (avoids needing git push rights, uses a fine-grained token)? The API
  approach is cleaner for GitHub-hosted control repos.
- If the control repo push fails (network outage, token expired), should steward treat this as a
  reconcile failure or a silent best-effort? Recommendation: log the error and continue — status
  writeback is observability, not a correctness requirement.
- What branch should the status file be committed to? `main` keeps everything in one place but
  mixes desired and observed state commits. A `status` branch is cleaner but adds complexity.

**Complexity:** Medium — the git commit/push is a few lines with gitpython, but the token
permission model and commit frequency decisions need to be settled first.

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

**Open questions:**
- Should `dry_run` be a global env var (node-wide) or a per-app manifest field? A node-wide
  dry-run is useful for initial deployment validation; per-app dry-run overlaps heavily with
  `sync_policy: manual`.
- Is the `steward_app_ooband_heal_total` metric sufficient, or should out-of-band heals also
  appear in the operation history (item 1.5) with a distinct `trigger: self_heal` field to
  distinguish them from git-triggered syncs?

**Complexity:** Low (documentation + log message + metric) to Medium (dry-run mode).

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

**Open questions:**

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

**Open questions:**
- Should v2 be introduced as part of this plan or deferred until there are enough new fields
  to justify a schema bump? The `compose_env_file` rename alone is worth doing; adding
  `sync_policy` and `pull_policy` at the same time means one bump covers all three.
- Should the deprecation warning for v1 `env_file` include a migration hint pointing to the
  docs?

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

**Open questions:**
- Should the default change from `always` to `missing` for backwards compatibility? `always`
  is safer (always gets latest) but more disruptive. `missing` is quieter but can miss tag
  updates. Recommendation: keep `always` as the default to preserve current behaviour.
- Should `pull_policy` be a manifest field (per app) or a node-level env var? Per-app is more
  useful since different stacks have different update cadences.

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

**Open questions:**
- Should `notify_url` also be settable per-app in the manifest, or only as a node-wide env var?
  Per-app allows routing different apps to different channels (e.g. critical apps to PagerDuty,
  others to ntfy). Node-wide is simpler.
- Should there be a notification for `OutOfSync` (drift detected, no sync triggered) in
  `sync_policy: manual` mode? This is the "someone needs to approve a sync" notification and
  maps directly to ArgoCD's notification for manual sync required.
- Should notifications be de-duplicated? If an app is stuck `Degraded` for 30 minutes,
  steward should not send 30 notifications. A simple "notify once per status transition" rule
  (only notify when status *changes* to a bad state, not on every cycle) is the right default.
  De-duplication requires tracking previous status — depends on SQLite state (item 3.1).

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

**Open questions:**
- `requirements.in` + `uv pip compile` (simpler, fits the current project size) vs a full
  `pyproject.toml` + `uv lock` (more idiomatic for a uv-first project but heavier). If items
  4.2 and 4.3 land together this decision becomes: adopt `pyproject.toml` once for all three.

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

**Complexity:** Low — mostly adding config files and CI steps.

---

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
- **Secrets management** — env files on the node filesystem are the secret boundary. Integration
  with Vault, SOPS, or similar is out of scope; steward is not a secrets operator.
