# steward

A lightweight, per-node GitOps reconciler for Docker Compose stacks, inspired by ArgoCD's reconciliation model.

## Concept

One container runs on each node. It watches a central **control repo** for app manifests, and per-app **stack repos** for compose file changes. When drift is detected it runs `docker compose up -d --remove-orphans` with the app's configured pull policy. Steward can reconcile its own stack as part of normal GitOps flow.

```
GitHub: homelab-gitops/          ← app manifests (what runs where)
GitHub: arr-stack/               ← docker compose files for arr
GitHub: prometheus-stack/        ← docker compose files for prometheus

On each node:
  steward (container)
    crond
      every minute  → reconcile stacks
```

---

## Prerequisites

- Docker with the `docker compose` plugin (v2) on each node
- A GitHub or GitLab account for your repos
- A container registry (GHCR recommended, free with GitHub)

---

## Quick start

The fastest path is the interactive setup script; a manual alternative follows.

### 1. Set up your control repo

See the companion **homelab-gitops** README for the control repo structure. Create a directory for your node under `nodes/` and add app manifests.

### 2. Run the setup script (recommended)

From the cloned steward repo:

```bash
./scripts/setup.sh
```

It prompts for your control-repo URL, branch, and node name, then:

- generates a dedicated **read-only** `ed25519` deploy key at `~/.ssh/steward_deploy_key` (never reusing your personal key);
- runs `ssh-keyscan` for the control host and **asks you to verify the fingerprint** against the provider's published list before pinning it (strict host-key checking);
- if this clone isn't already at `<STEWARD_DATA_DIR>/stacks/steward` (see [Bootstrap](#bootstrap)), asks for `STEWARD_DATA_DIR` and offers to provision a clone there for you, so config isn't lost on the first self-update;
- writes `.env` (with your host UID/GID and the resolved `STEWARD_DATA_DIR`), `credentials.yml`, and `docker-compose.override.yml` next to that clone — all gitignored;
- prints the public key to register and the steps to add more repos.

Add the printed public key as a **read-only deploy key** on your control repo (GitHub: per repository; GitLab: *Settings → Repository → Deploy keys*). Then start the agent and validate:

```bash
docker compose up -d
./scripts/doctor.sh
```

> **More repos:** GitHub forbids reusing one deploy key across repositories, so each additional GitHub stack repo needs its own key and SSH host alias. The script prints the exact steps — see [Advanced: per-repo & multi-host keys](#advanced-per-repo--multi-host-keys).

### Manual setup (alternative)

If you prefer to wire things up by hand:

1. **`.env`** — clone steward into `<STEWARD_DATA_DIR>/stacks/steward` (see [Bootstrap](#bootstrap)), then from inside that clone run `cp .env.example .env` and set `CONTROL_REPO_URL`, `GITOPS_NODE_NAME`, and `STEWARD_DATA_DIR` to the absolute path whose `stacks/steward` subdirectory is this clone (e.g. if the clone is `/opt/steward/stacks/steward`, set `STEWARD_DATA_DIR=/opt/steward`). Add your host identity so files are not root-owned:
   ```bash
   echo "STEWARD_UID=$(id -u)" >> .env
   echo "STEWARD_GID=$(id -g)" >> .env
   ```
   SSH (`git@host:path`, `ssh://host/path`) and plain HTTPS are supported; HTTPS URLs with embedded credentials are **not**.

2. **Deploy key** — generate a dedicated key (do not reuse `~/.ssh/id_ed25519`):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/steward_deploy_key -N "" -C "steward@$(hostname)"
   ```
   Add `~/.ssh/steward_deploy_key.pub` as a **read-only deploy key**. Read-only is the default on both GitHub and GitLab and is sufficient for every repo — steward never writes to git. (A GitLab *deploy key* (SSH) is not a GitLab *deploy token* (HTTPS); steward uses the deploy key.)

3. **Host-key trust** — pin the control host's key (strict). Verify the fingerprint against the provider's published list first:
   ```bash
   ssh-keyscan -t rsa,ecdsa,ed25519 github.com > ~/.ssh/steward_known_hosts
   ```
   Referencing a `known_hosts` file switches steward to `StrictHostKeyChecking yes`, so it **must** contain the host key or clones fail with `Host key verification failed`. Omit it to fall back to `accept-new` (trust on first use).

4. **`credentials.yml`** — `cp credentials.yml.example credentials.yml` and point the entry at your control host:
   ```yaml
   credentials:
     - pattern: github.com
       key_file: /run/secrets/control_key
   known_hosts_file: /run/secrets/ssh_known_hosts
   ```

5. **`docker-compose.override.yml`** — `cp docker-compose.override.yml.example docker-compose.override.yml` and set the `file:` paths under `secrets:` to your key and `known_hosts` paths. It mounts `credentials.yml` read-only and declares each key as a Docker secret.

6. **Start & validate**:
   ```bash
   docker compose up -d
   ./scripts/doctor.sh
   ```

The diagnostic script checks host config and the running container, performs a live `git ls-remote` against the control repo, and prints a pass / warn / fail summary (exiting non-zero on failure). It catches the misconfigurations that otherwise cost hours — a wrong host in `CONTROL_REPO_URL` (e.g. `github.com` where `gitlab.com` was meant), an empty or mismatched `known_hosts`, or a missing deploy key.

---

## Advanced: per-repo & multi-host keys

`credentials.yml` maps each git host — or a per-repo **SSH alias** — to its own deploy key. steward turns each entry into a `Host` block in the container's `~/.ssh/config`; key selection is done entirely by SSH.

### Multiple git hosts

When the control repo and app repos live on different hosts (e.g. GitHub + GitLab), give each host its own key:

```yaml
credentials:
  - pattern: github.com
    key_file: /run/secrets/github_key
  - pattern: gitlab.com
    key_file: /run/secrets/gitlab_key
# Optional. If set, steward enforces StrictHostKeyChecking=yes and this file
# MUST contain the host key for every host above. Remove it to use accept-new.
known_hosts_file: /run/secrets/ssh_known_hosts
```

### Multiple repos on the same host (per-repo keys)

GitHub rejects reusing a deploy key across repositories, so each GitHub repo needs its **own** key. Because SSH selects keys by host, give each extra repo a unique **host alias** via `hostname`:

```yaml
credentials:
  - pattern: github.com            # control repo — the default github.com key
    key_file: /run/secrets/control_key
  - pattern: github.com-arr        # alias for the arr stack repo (must be unique)
    hostname: github.com           # the real host the alias connects to
    key_file: /run/secrets/arr_key
known_hosts_file: /run/secrets/ssh_known_hosts
```

Then reference the alias in that app's manifest so SSH picks the matching key:

```yaml
repo: git@github.com-arr:you/arr-stack.git
```

`known_hosts` still only needs the real host (`github.com`) — aliases resolve to it via `HostName`.

For every key, declare a matching Docker secret in `docker-compose.override.yml`:

```yaml
services:
  steward:
    secrets:
      - control_key
      - arr_key
      - ssh_known_hosts

secrets:
  control_key:
    file: /home/you/.ssh/steward_deploy_key
  arr_key:
    file: /home/you/.ssh/steward_arr_key
  ssh_known_hosts:
    file: /home/you/.ssh/steward_known_hosts
```

When `credentials.yml` is present, steward uses it and ignores the legacy single-key `ssh_key` secret.

---

## Configuration

All configuration is via environment variables.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONTROL_REPO_URL` | yes | — | SSH URL of the control repo (`git@host:path` or `ssh://host/path`) |
| `CONTROL_REPO_BRANCH` | no | `main` | Branch to track on the control repo |
| `STEWARD_DATA_DIR` | no | `./steward-data` | Host path where git repos are cloned (outside the container) |
| `STEWARD_UID` | no | `0` (root) | UID to run steward as — set to your host user's UID so files in `STEWARD_DATA_DIR` are not root-owned |
| `STEWARD_GID` | no | `STEWARD_UID` | GID to run steward as — defaults to `STEWARD_UID` if not set |
| `GITOPS_ROOT` | no | `/git` | Repo root inside the container — change only if you remap the volume |
| `GITOPS_NODE_NAME` | no | `hostname` | Node name, must match `nodes/<name>` in control repo |
| `AGENT_CONTAINER_NAME` | no | `steward` | Container name — used by `docker inspect` for debug path logging and to detect when reconciling steward's own stack (self-update via helper container) |
| `LOGLEVEL` | no | `INFO` | Log level (`DEBUG` enables per-path inside/outside diagnostics) |
| `METRICS_PORT` | no | — (disabled) | Port for the Prometheus `/metrics` scrape endpoint; unset to disable |
| `STEWARD_NOTIFY_URL` | no | — (disabled) | Default webhook endpoint for failure/degraded/drift notifications |
| `STEWARD_DRY_RUN` | no | `false` | Node-wide read-only mode: detect/report drift, but never run `docker compose up` |

---

## App manifest schema

Each `.yml` file in `nodes/<hostname>/` in the control repo describes one application.

> **Paths in manifests are container-internal.** Steward mounts `STEWARD_DATA_DIR` (host) as `/git` inside the container. Stack repos are cloned to `/git/stacks/<name>/`. So if your `.env` is committed to the stack repo, the path is `/git/stacks/<name>/.env`. Do **not** use host paths like `/home/ubuntu/git/...` — those do not exist inside the steward container.

```yaml
version: 2                              # required, supported: 1, 2
name: arr                               # required, used as local repo directory name
repo: git@github.com:you/arr-stack.git  # required, SSH or plain HTTPS
ref:
  branch: main                          # mutually exclusive with tag
  # tag: v1.2.3                         # pin to a specific release
path: .                                 # path within repo to compose file, default: .
compose_file: docker-compose.yml        # compose filename, default: docker-compose.yml
compose_env_file: /git/stacks/arr/.env  # container-internal path; see note above
sync_policy: auto                       # optional: auto (default) or manual
pull_policy: always                     # optional: always (default), missing, never
health_check_delay_seconds: 30          # optional: delay before compose health check
notify_url: https://notify.example/hook # optional: per-app webhook override
enabled: true                           # required, explicit — use false to disable without deleting
```

Compatibility notes:

- v1 manifests are still accepted.
- `env_file` still works but is deprecated; steward logs a warning and you should migrate to `compose_env_file`.
- `compose_env_file` and `env_file` cannot be set together.

### Validation rules

- `version`, `name`, `repo`, `ref`, and `enabled` are all required — missing any is a hard error
- `ref.branch` and `ref.tag` are mutually exclusive — specifying both is a hard error
- `enabled` has no default — must be explicitly set
- `sync_policy` supports `auto` and `manual` (default: `auto`)
- `pull_policy` supports `always`, `missing`, and `never` (default: `always`)
- `health_check_delay_seconds` must be an integer between `0` and `600` (default: `30`)
- `notify_url` overrides `STEWARD_NOTIFY_URL` for that app only

---

## Private repos (SSH only)

Steward exclusively uses SSH deploy keys for private repo access. HTTPS URLs with embedded tokens are **not supported**.

### credentials.yml (recommended)

The setup script and the recommended manual path both use `credentials.yml`, which maps each git host (or per-repo alias) to a Docker-secret key file and is mounted read-only into the container:

```yaml
services:
  steward:
    volumes:
      - ./credentials.yml:/app/credentials.yml:ro
    secrets:
      - control_key
      - ssh_known_hosts

secrets:
  control_key:
    file: /home/you/.ssh/steward_deploy_key
  ssh_known_hosts:
    file: /home/you/.ssh/steward_known_hosts
```

Declare secrets in `docker-compose.override.yml` — **not** the base `docker-compose.yml` (which has no hardcoded secret paths, to avoid startup failures on hosts where the files don't exist). For host aliases, multiple keys, and the manifest URL changes they require, see [Advanced: per-repo & multi-host keys](#advanced-per-repo--multi-host-keys).

### Legacy single-key secret

Without `credentials.yml`, steward falls back to a single `ssh_key` secret applied to all hosts:

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
    file: /home/you/.ssh/known_hosts
```

The entrypoint reads `/run/secrets/ssh_key` at startup and configures SSH automatically. This path cannot do per-repo keys — prefer `credentials.yml`.

### Use SSH URLs in manifests

```yaml
repo: git@github.com:you/arr-stack.git
```

---

## Self-update

Steward updates itself the same way it updates any other app: through git. The image version is pinned directly in `docker-compose.yml`. When you push a `v*` release tag, the `bump-self-image` CI job opens a PR that bumps the pin to the new version. Merging the PR causes steward to detect a change in its own stack repo and trigger an update.

### Why a helper container?

Running `docker compose up -d` from inside a container replaces that container — Docker sends SIGTERM to the old container before the new one starts, which kills the process that initiated the update. To avoid this, steward detects when it is reconciling its own stack (by comparing `app.name` to `AGENT_CONTAINER_NAME`) and spawns a short-lived **helper container** instead:

```
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v <STEWARD_DATA_DIR>:<STEWARD_DATA_DIR> \
  -e HOME=/tmp \
  <current-steward-image> \
  sh -c "sleep 5 && docker compose --project-name <app.name> -f <compose-file> up -d --remove-orphans --pull always"
```

The helper is a peer container, independent of steward's process. When Docker Compose stops steward, the helper is unaffected and creates the new container cleanly. The 5-second delay lets the old container exit fully before `compose up` runs.

If host paths cannot be resolved (e.g. `AGENT_CONTAINER_NAME` is wrong or docker inspect fails), steward falls back to calling `docker compose --project-name <app.name> up -d` directly — which will kill itself, but `restart: unless-stopped` ensures the container comes back up on the next Docker restart cycle.

### Bootstrap

The first time you deploy steward on a node, clone the steward repo into `STEWARD_DATA_DIR/stacks/steward` manually, create a `.env` and `docker-compose.override.yml` there for node-specific settings (SSH mounts, ports, etc.), and start the container. Once steward is running and its own stack repo is in `STEWARD_DATA_DIR/stacks/steward`, it will manage its own updates from that point on.

Add a manifest for steward itself in the control repo under `nodes/<hostname>/`:

```yaml
version: 1
name: steward
repo: https://github.com/asksven/steward
ref:
  branch: main
path: .
compose_file: docker-compose.yml
enabled: true
```

---

## Upgrade notes

### Upgrading from 0.4.4 — remove stale control-repo status files (one-time)

Versions up to 0.4.4 wrote an observed-state snapshot back to the control repo at
`nodes/<hostname>/status.json`. That writeback has been **removed** — steward now exposes
observed state via Prometheus + SQLite only, and the control repo holds *desired* state
exclusively (and a **read-only** deploy key is now sufficient).

After rolling the new image out to **every** node, the code stops touching the control repo and
any node wedged on a divergent control-repo commit self-heals on its next reconcile. The
`status.json` files committed by older versions remain, though, so do this one-time cleanup
**by hand** (steward will never delete them):

1. **Check nothing still reads them.** In the control repo:
   ```bash
   grep -rn "status.json" .
   ```
   Repoint any dashboard or tooling that parses `status.json` at the Prometheus `/metrics`
   endpoint instead.

2. **Delete the stale files in a single human commit:**
   ```bash
   git pull
   find nodes -name status.json        # confirm the paths first
   git rm nodes/*/status.json
   git commit -m "Remove stale steward status.json (writeback removed in >0.4.4)"
   git push
   ```
   This is **one** commit total for the whole fleet — all nodes share the one control repo, so
   it is not a per-node step.

3. **Confirm it stays clean.** On a node host, after the next reconcile cycle:
   ```bash
   git -C /git/control status                    # clean, no status.json
   git -C /git/control rev-parse HEAD
   git -C /git/control rev-parse origin/main     # should match
   ```

---

## Logs

```bash
docker logs -f steward
```

All output (cron + steward) is forwarded to the container's stdout via `/proc/1/fd/1`.

Set `LOGLEVEL=DEBUG` in your `.env` to enable verbose path diagnostics. At DEBUG level, steward logs both the container-internal path and the corresponding host path for every file it touches (repos, compose files, env files), plus a full mount map at startup. This is useful for diagnosing `env_file` or repo path mismatches:

```env
LOGLEVEL=DEBUG
```

---

## Troubleshooting

### `Host key verification failed`

Full error, e.g.:

```
No ED25519 host key is known for github.com and you have requested strict checking.
Host key verification failed.
```

**Cause:** a `known_hosts` file is referenced (the `ssh_known_hosts` secret, or `known_hosts_file` in `credentials.yml`), which forces `StrictHostKeyChecking yes` — but the file is empty or missing the key for the host steward is cloning from.

**Fix — pick one:**

- Populate the `known_hosts` file with the host's key, then restart:
  ```bash
  ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> ~/.ssh/steward_known_hosts
  ```
- Or stop referencing it (remove `known_hosts_file` from `credentials.yml`, or drop the `ssh_known_hosts` secret) to fall back to `accept-new`.

**Diagnose inside the container:**

The quickest check is `scripts/doctor.sh`, which resolves the effective SSH
config and verifies the host key automatically. To inspect manually, note that
`docker exec` defaults to **root** (so `~` is `/root`), but steward runs as a
non-root user with `HOME=/home/steward` — read the config from there:

```bash
# StrictHostKeyChecking value + which key/known_hosts apply (fall back to /root
# if steward runs as root):
docker exec steward sh -c 'cat /home/steward/.ssh/config 2>/dev/null || cat /root/.ssh/config'
```

With `credentials.yml`, the known_hosts file lives at the path in
`UserKnownHostsFile` (the secret path, **not** `~/.ssh/known_hosts`). Resolve it
and confirm it has the control-repo host key:

```bash
# Replace gitlab.com with your control-repo host (see CONTROL_REPO_URL):
docker exec steward sh -c 'ssh -G gitlab.com | awk "\$1==\"userknownhostsfile\"{print \$2; exit}"'
docker exec steward sh -c 'ssh-keygen -F gitlab.com -f /run/secrets/ssh_known_hosts'
```

---

### `Permission denied (publickey)`

Full error, e.g.:

```
git@gitlab.com: Permission denied (publickey).
fatal: Could not read from remote repository.
```

This appears **after** host-key trust passes, so it is easy to misread as a key
problem when the real cause is a wrong host. Common causes:

- **Wrong host / typo in `CONTROL_REPO_URL`** — e.g. `git@github.com:...` where
  the repo lives on `gitlab.com`. The deploy key is unknown on the wrong host,
  so it returns `Permission denied`. Run `scripts/doctor.sh`; it echoes the
  resolved host explicitly.
- **Deploy key not registered** with the repo (or lacking access). Add the
  public key as a deploy key on the repo.
- **Don't test with `ssh -T git@host`** — deploy keys are repo-scoped and always
  return `Permission denied (publickey)` even on a working node. Test with
  `git ls-remote <repo-url>` instead (which is exactly what `doctor.sh` does):
  ```bash
  docker exec -u "$(docker exec steward printenv STEWARD_UID)" \
    -e HOME=/home/steward steward git ls-remote "$(docker exec steward printenv CONTROL_REPO_URL)"
  ```

---

## Metrics

Set `METRICS_PORT` in your `.env` to start a Prometheus scrape endpoint inside the container:

```env
METRICS_PORT=9101
```

Expose the port via `docker-compose.override.yml`:

```yaml
services:
  steward:
    ports:
      - "9101:9101"
```

Then add a scrape config to your Prometheus:

```yaml
scrape_configs:
  - job_name: steward
    static_configs:
      - targets:
          - media-1:9101   # one entry per node
          - media-2:9101
```

### Exposed metrics

| Metric | Type | Description |
|---|---|---|
| `steward_reconcile_last_timestamp_seconds` | gauge | Unix timestamp of last completed reconciliation run |
| `steward_reconcile_duration_seconds` | gauge | Duration of last reconciliation run |
| `steward_reconcile_total{result}` | counter | Reconciliation runs by result (`success`, `partial_failure`, `fatal`) |
| `steward_control_repo_sync_total{result}` | counter | Control repo sync attempts by result (`up_to_date`, `updated`, `failed`) |
| `steward_manifest_parse_errors_total` | counter | Total manifest parse errors encountered |
| `steward_app_info{app,repo,ref,ref_type,enabled}` | gauge | Static app information (always 1) |
| `steward_app_last_reconcile_timestamp_seconds{app}` | gauge | Unix timestamp of last reconcile attempt per app |
| `steward_app_last_sync_timestamp_seconds{app}` | gauge | Unix timestamp of last `docker compose up` per app |
| `steward_app_reconcile_total{app,result}` | counter | Reconcile attempts per app by result (`success`, `failed`, `skipped`) |
| `steward_app_sync_total{app,result}` | counter | Compose runs per app by result (`success`, `failed`) |
| `steward_app_sync_status{app,status}` | gauge | Current sync status as one-hot values across `Synced`, `OutOfSync`, `Unknown`, `Disabled` |
| `steward_app_health_status{app,status}` | gauge | Current health status as one-hot values across `Healthy`, `Degraded`, `Progressing`, `Unknown` |
| `steward_app_ooband_heal_total{app}` | counter | Out-of-band drift heals performed by steward (`sync_policy=auto` and not dry-run) |

All metrics carry a `node` label set to `GITOPS_NODE_NAME`.

### Grafana alerts

| Alert | Expression | Severity |
|---|---|---|
| Compose apply failed | `increase(steward_app_sync_total{result="failed"}[5m]) > 0` | critical |
| Repeated reconcile failures | `increase(steward_app_reconcile_total{result="failed"}[15m]) > 2` | warning |
| Node not reporting | `time() - steward_reconcile_last_timestamp_seconds > 300` | critical |
| App not reconciled | `time() - steward_app_last_reconcile_timestamp_seconds > 300` | warning |
| Manifest parse error | `increase(steward_manifest_parse_errors_total[5m]) > 0` | warning |
| Control repo sync failing | `increase(steward_control_repo_sync_total{result="failed"}[15m]) > 0` | warning |
| Reconcile partial failure | `increase(steward_reconcile_total{result="partial_failure"}[15m]) > 0` | warning |

---

## Reconciler flow (per cron run)

```
1. git fetch control repo
   └── if changed: pull, re-read node manifests

2. for each app manifest (including steward itself, if present):
   ├── enabled: false → skip
  │                  └── sync status: Disabled
   ├── stack repo not cloned → clone
   ├── git fetch stack repo
  │   ├── compare failed → sync status: Unknown
  │   ├── up to date
  │   │   ├── no live drift → sync status: Synced
  │   │   └── live drift (missing/stopped expected service)
  │   │       ├── sync_policy: manual → skip apply, notify drift_detected
  │   │       └── sync_policy: auto
  │   │           ├── STEWARD_DRY_RUN=true → skip apply, notify drift_detected
  │   │           └── STEWARD_DRY_RUN=false → self-heal via compose up
  │   └── changed
  │       ├── sync_policy: manual → skip apply (sync status: OutOfSync), notify drift_detected
  │       └── sync_policy: auto
  │           ├── STEWARD_DRY_RUN=true → skip apply, notify drift_detected
  │           └── STEWARD_DRY_RUN=false → git pull/checkout → docker compose --project-name <app.name> up -d --remove-orphans --pull <pull_policy>
   └── log result
```

> Steward never writes to the control repo. Observed state (sync/health status,
> deployed SHAs, operation history) is exposed via the Prometheus `/metrics`
> endpoint and the local SQLite state store only — git holds *desired* state
> exclusively.

### Notifications

When a notification URL is configured (`notify_url` per app, or global `STEWARD_NOTIFY_URL`), steward posts JSON events for:

- `sync_failed`
- `health_degraded`
- `drift_detected` (including manual mode OutOfSync)

Notifications are intentionally **not de-duplicated**. If a bad state persists, a notification is sent on each reconcile cycle.

---

---

## Developer guide

### Repository structure

```
steward/
  steward.py          # Main steward logic
  Dockerfile             # Agent container image
  docker-compose.yml     # Deploy the agent on a node
  crontab                # Cron schedule (reconcile)
  entrypoint.sh          # Container entrypoint
  examples.yml           # Example app manifests
  .github/
    workflows/
      build.yml          # Lint + test + build and push image to GHCR
```

### Development setup

Steward uses **uv** for local dependency management and developer commands.

```bash
uv venv
uv pip install -r requirements.txt pytest ruff
uv run pytest -v --tb=short
uv run ruff check steward.py metrics_server.py tests
```

Runtime dependencies are managed with:

- `requirements.in` for direct dependencies
- `requirements.txt` for pinned, reproducible installs

To refresh pinned versions:

```bash
uv pip compile requirements.in -o requirements.txt
```

### Testing a local build

To run a locally built image without changing `docker-compose.yml`, set `AGENT_IMAGE` in the `.env` file next to your deployment:

```env
AGENT_IMAGE=ghcr.io/<you>/steward:dev
```

This overrides the pinned tag for that node only and is not tracked by the bump workflow.

### Releasing

The GitHub Actions workflow (`build.yml`) triggers on two events:

| Trigger | Condition |
|---|---|
| Push to `main` | Only when files under `steward/` change |
| Push of a `v*` tag | Always |

#### Image tags produced

| Event | Tags produced |
|---|---|
| Push to `main` | `main`, `sha-<sha>`, `latest` |
| Push of `v0.1.0` | `0.1.0`, `sha-<sha>` |

`latest` is only added on `main` pushes, not on tag pushes.

#### How to cut a release

```bash
# Ensure main is up to date and all changes are committed
git push origin main          # triggers build → updates :latest, :main

# Tag the release
git tag v0.1.0
git push origin v0.1.0        # triggers build → creates :0.1.0, :sha-<sha>
```

Push both in the same command if you prefer:

```bash
git tag v0.1.0
git push origin main v0.1.0
```

The image tag in `docker-compose.yml` is the source of truth. The `bump-self-image` CI job opens a PR on every `v*` tag (pinning the v-stripped version, e.g. `0.1.0`, to match the published GHCR tag); merging it triggers a self-update on the next reconcile cycle.
