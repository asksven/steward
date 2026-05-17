# steward — future development plan

Findings from an ArgoCD-consistency and Docker Compose idiom review.
Items are grouped by theme and ordered roughly by impact within each group.

---

## 1. Live state drift detection

**Problem:** Steward only acts when the git SHA advances. If containers are stopped manually,
crash-loop, or are removed outside of steward, no action is taken until the next git commit.
This is the biggest gap vs ArgoCD's self-healing model.

**Proposed solution:** On each reconcile cycle, after the git check, query the live state of the
stack (`docker compose ps --format json`) and compare it against the expected services from the
compose file. If any service is missing, stopped, or in a restart loop, trigger `compose up`
regardless of whether git changed.

**Complexity:** Medium — requires parsing compose ps output and defining what "healthy" means
per service type (oneshot vs long-running).

---

## 2. Health status separate from sync status

**Problem:** Steward only checks whether `docker compose up` exited 0, not whether the resulting
containers are actually running and healthy. A compose up can succeed while all containers
immediately crash-loop (e.g. bad env var, missing mount).

**Proposed solution:** After a successful compose up, run `docker compose ps --format json` and
check that all non-oneshot services have `State=running`. Report a distinct result (`synced` vs
`synced_unhealthy`) and expose a separate `steward_app_health` gauge metric
(`1=healthy`, `0=degraded`).

**Complexity:** Medium — depends on item 1 above for the live state query logic.

---

## 3. Deployed revision tracking

**Problem:** There is no record of what git SHA is currently deployed to each app. You cannot
tell from steward's state whether the running stack matches what is in git.

**Proposed solution:** After a successful `compose up`, persist the deployed SHA in the metrics
state file (`state["apps"][name]["deployed_sha"]`). Expose it via a `steward_app_deployed_info`
gauge label alongside the remote SHA (already available from git). This gives an OutOfSync signal
analogous to ArgoCD's.

**Complexity:** Low — purely additive to existing state persistence.

---

## 4. Reconcile / sync terminology cleanup

**Problem:** `reconcile_app` does both drift detection and apply in one function. The metric
names hint at the distinction (`steward_app_sync_total` vs `steward_app_reconcile_total`) but
the code conflates the two phases, making it hard to reason about which step failed.

**Proposed solution:** Split `reconcile_app` into two clearly named phases:
- `check_app(app, state) -> SyncStatus` — git fetch, SHA comparison, return `in_sync / out_of_sync / check_failed`
- `sync_app(app, state) -> bool` — compose up, health check, update deployed SHA

`reconcile_app` becomes a thin orchestrator that calls both and records the results separately.

**Complexity:** Low-medium — refactor only, no new external behaviour.

---

## 5. Fix `env_file` semantics

**Problem:** The manifest `env_file` field is a path inside the steward container passed as
`docker compose --env-file`, which controls *compose variable substitution* — not container
environment variables. This is different from what users familiar with Docker Compose's native
`env_file:` service key expect (which injects variables into the container). The naming collision
is a footgun.

**Options:**
- **Rename to `compose_env_file`** in a v2 manifest schema to make the substitution semantics
  explicit. Keep `env_file` as a deprecated alias.
- **Document the distinction clearly** in the manifest schema section of the README (short-term).

**Complexity:** Low (docs) / Low-medium (schema v2 rename with deprecation shim).

---

## 6. Explicit `--project-name` for compose runs

**Problem:** Docker Compose derives the project name from the working directory name. This
currently works because repos are cloned into `STACKS_DIR/<app.name>`. It is an implicit
assumption that will break if the directory layout ever changes.

**Proposed solution:** Always pass `--project-name app.name` to `docker compose up` (and any
other compose invocations added in future). This makes the project name an explicit contract
matching the manifest name.

**Complexity:** Trivial.

---

## 7. Smarter image pulling

**Problem:** `--pull always` is passed on every `compose up`, which re-pulls every image in the
stack whenever any file in the repo changes (including non-image changes like README edits).

**Options:**
- **`--pull missing`** (default Docker Compose behaviour) — only pull if the image is not
  present locally. Simpler but misses tag updates.
- **Separate pull phase** — run `docker compose pull` before `up`, capture which images actually
  changed, and only restart services whose image digest changed. Most correct but more complex.
- **Make it configurable** — add a `pull_policy: always | missing | never` field to the manifest
  (default `always` for backwards compatibility).

**Complexity:** Low (configurable field) to Medium (separate pull phase with digest comparison).

---

## 8. Failure notifications (pre-existing backlog item)

**Problem:** Reconciliation failures are visible in logs and metrics but there is no active
notification. A node can be failing silently for an hour before the next Prometheus alert fires.

**Proposed solution:** Add an optional `notify_url` field to `.env` or the manifest.
On any `result=failed` event, POST a JSON payload to the URL (compatible with ntfy, Gotify,
Slack incoming webhooks). Keep it opt-in with no default.

**Complexity:** Low.

---

## 9. GitOps self-update via Dependabot ✓ implemented

**Decisions made:**
- Deployment `docker-compose.yml` stays in the steward source repo; Dependabot PRs land there.
- Dependabot (not Renovate) chosen for simplicity on GitHub.
- Bootstrap: one-time manual clone of the steward repo into `STEWARD_DATA_DIR/stacks/steward`,
  create node-local `.env` and `docker-compose.override.yml` there, then add a steward manifest
  to the control repo. From that point steward manages its own updates.
- Recovery when steward is broken: manual `docker compose up` on the host — accepted, same as
  recovering a broken ArgoCD server.
- `AGENT_IMAGE` env var retained as a testing override (overrides the pinned tag in
  `docker-compose.yml` for that node without touching git).

**What was removed:** `self_update()` function, `AGENT_IMAGE` env var pass-through in
`docker-compose.yml`, `0 * * * *` self-update cron entry in both `crontab` and the runtime
crontab heredoc, `steward_self_update_total` metric, rolling-tag guidance in README.

**What was added:** `.github/dependabot.yml` watching the Docker ecosystem daily.

---

## 10. Self-update: helper container pattern

**Problem:** When steward reconciles its own stack, it runs `docker compose up -d` from inside its
own container. Docker Compose sends `SIGTERM` to the running container (steward itself) before the
replacement container is started. The in-flight `compose up` process is killed along with the
container, so the new container is never created. `restart: unless-stopped` does not help because
Docker marks a container stopped by Compose as intentionally stopped. The result: steward stops,
the new image is never pulled, and the only recovery is a manual `docker compose up -d` on the
host.

**Proposed solution (Option B — helper container):**
When steward detects it is reconciling its own stack (i.e. `app.name` matches the running
container name from `AGENT_CONTAINER_NAME`), instead of calling `docker compose up -d` directly,
it spawns a short-lived helper container via the Docker socket:

```python
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v <stack_dir>:<stack_dir> \
  -w <stack_dir> \
  docker:cli \
  sh -c "sleep 5 && docker compose up -d --remove-orphans --pull always"
```

The helper runs in a separate container that is not steward. When Docker Compose stops steward, the
helper container is unaffected. After the 5-second delay (long enough for steward to be fully
stopped and Docker's bookkeeping to settle), the helper runs `compose up` which creates the new
steward container and exits cleanly.

**Implementation notes:**
- Detecting the self-update case: check `os.environ.get("AGENT_CONTAINER_NAME")` and compare to
  `app.name` (both default to `steward`).
- The helper needs the stack directory mounted at the same absolute path so that relative volume
  paths in `docker-compose.yml` resolve correctly.
- The `docker:cli` image is a natural choice for the helper as it contains only the Docker CLI.
  Alternatively, the running steward image itself could be used (it already has `docker-cli` and
  `docker-cli-compose`).
- `env_file` pass-through: if the stack has a compose env-file, it must also be mounted into the
  helper container.
- The helper container must be removed on exit (`--rm`) so it does not accumulate.

**Complexity:** Medium — requires detecting the self-update case, building the correct `docker run`
invocation with volume mounts, and testing the timing window. The core mechanism is straightforward
but the mount resolution is fiddly.

---

## Non-goals

These ArgoCD concepts are deliberately out of scope for steward's design:

- **Rollback** — git revert is the rollback mechanism; steward tracks forward only.
- **Sync waves / ordering** — compose files declare dependencies via `depends_on`; steward
  delegates ordering to compose.
- **Multi-cluster / multi-node coordination** — one steward per node is the intended model.
- **UI / web dashboard** — Grafana dashboards cover the observability need.
