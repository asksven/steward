# Plan: Fix relative bind-mount paths in managed stacks (Option B — transparent)

Status: **ready for implementation**. Target repo: `asksven/steward` (this repo).
Executor: implement exactly as written; do not change the deployment model
(no migration). Follow the repo conventions in `.github/copilot-instructions.md`
(use `uv`, run ruff + pytest after changes).

---

## 1. Problem

Steward-managed stacks whose compose file bind-mounts files **from the repo via
relative paths** (e.g. `./ipsec.conf:/etc/ipsec.conf:ro`) fail to start:

```
error mounting "/git/stacks/<app>/<sub>/ipsec.secrets" ... not a directory:
Are you trying to mount a directory onto a file
```

Root cause: `run_compose()` runs `docker compose up` **inside** the steward
container using container-internal paths (`GITOPS_ROOT`, default `/git`). A
relative bind source like `./ipsec.secrets` resolves — inside steward — to
`/git/stacks/<app>/<sub>/ipsec.secrets`. That absolute path is passed to the
**host** Docker daemon, which has no `/git`, so it auto-creates empty
directories there and the file-mount fails. Absolute host-path bind mounts are
unaffected, which is why only repo-file-mounting stacks break.

## 2. Chosen approach (Option B, confirmed)

Steward **already** solves this for self-update. `spawn_compose_helper()`
(steward.py:748) resolves host paths via `_resolve_host_path()` (steward.py:102)
and runs a **peer container** with `-v {host_root}:{host_root}` and
`-f {host_compose_file}`. Inside that peer the host path is valid, so Compose
reads the file and resolves relative bind sources to host paths correctly.

**Generalize that mechanism to all apps.** `run_compose()` gains a peer-execution
path when the container path differs from the host path; self-update keeps its
detached peer. Fully transparent: no change to how steward is mounted, no
manifest changes, no migration. The manual `/git` symlink becomes unnecessary.

Do **not** implement Option A (changing the default mount / `GITOPS_ROOT`).

---

## 3. Implementation steps

### Step 0 — Read the current code
Read in `steward.py`: `_container_mounts()` (56), `_find_best_mount()` (72),
`host_path()` (86), `_resolve_host_path()` (102), `log_mounts()` (115); the
`is_self` check `return app.name == AGENT_CONTAINER_NAME` (~729);
`spawn_compose_helper()` (748); `_compose_file_args()` (857); `run_compose()`
(874); startup block in `main()` (~1528-1543). Identify the call site(s) that
branch on `is_self` to choose `spawn_compose_helper()` vs `run_compose()`; keep
that branching intact.

### Step 1 — Extract shared helpers
1. Add a small `PeerComposePaths` dataclass with `host_root`, `host_workdir`,
   `compose_files`, `env_file`, and `bind_specs`. `compose_files` contains raw
   host path strings only, with no `-f` markers or shell quoting.
2. `_resolve_compose_host_paths(app, stack_path) -> Optional[PeerComposePaths]`
   - Resolve `GITOPS_ROOT` and the app workdir (`stack_path/app.path`). Mounting
     both paths makes the compose project directory visible even if it is
     covered by a more specific nested mount than `GITOPS_ROOT`.
   - Resolve `stack_path/app.path/app.compose_file`; if
     `docker-compose.override.yml` exists, resolve and append it (mirror
     `_compose_file_args`).
   - If `app.env_file` is set, first require the container path to exist, then
     resolve it.
   - Build deduplicated `bind_specs` for `host_root` and `host_workdir`. For
     every compose, override, or env file whose resolved host path is not below
     either bound directory, add a read-only file bind at the same absolute
     path. This covers inputs supplied through separate or more-specific mounts.
   - Return `None` if **any configured input** cannot be resolved: host root,
     app workdir, main compose file, a present override file, or a configured
     env file. Never silently omit an override or env file and run a changed
     stack definition.
3. `_build_compose_up_cmd(app, compose_files: list[str], env_file: Optional[str]) -> list[str]`
   - `["docker","compose","--project-name", app.name, ("-f", f)…,
     ("--env-file", env_file)?, "up","-d","--remove-orphans","--pull", app.pull_policy]`.
   - Inputs and output are unquoted argv strings. Used for BOTH direct and peer
     commands so flags never drift; `_run_peer_compose()` owns shell quoting.
4. `_run_peer_compose(app, inner_cmd, bind_specs, *, detach: bool, delay: int) -> CompletedProcess | None`
   - Builds `docker run --rm [--detach] --entrypoint sh -v /var/run/docker.sock:/var/run/docker.sock
     (-v {bind_spec})… -e HOME=/tmp {helper_image} -c "[sleep {delay} &&] timeout 300 {shlex-joined inner}"`.
   - Consume the already deduplicated directory and read-only file bind specs
     from `PeerComposePaths`.
   - `helper_image = _get_helper_image()`; if falsy, return `None`. This is the
     **only** meaning of `None`: no peer image is available.
   - `detach=True` (self): `-d`, `sleep {delay} &&`, outer `timeout 30`.
   - `detach=False` (regular): no `-d`, no sleep, outer `timeout 310`.
   - Apply `shlex.quote()` exactly once to every raw inner argv item before
     joining it for `sh -c`.
   - Do not catch `subprocess.TimeoutExpired` or `FileNotFoundError`; callers
     retain their context-specific logging and `bool` return behavior.

Refactor `spawn_compose_helper()` to use these helpers with
`detach=True, delay=5`; preserve its existing self-update fallback-to-`run_compose`
and log messages.
This fallback is specific to the self-update restart model and must not be
copied to the regular-app peer path.

### Step 2 — Add the peer path to `run_compose()`
1. Keep the `compose_file.exists()` check + inside/outside debug logging.
2. Decide mode:
   ```python
   host_root = _resolve_host_path(GITOPS_ROOT)
   use_peer = host_root is not None and host_root != str(GITOPS_ROOT)
   ```
3. **Direct path** (`use_peer` False): keep the existing body verbatim
   (container `-f` paths, `--env-file` container path, `cwd=stack_path/app.path`,
   `subprocess.run(timeout=300)`, `[compose/<app>]` logging, returncode). This is
   also the compatibility path when `docker inspect` cannot resolve
   `GITOPS_ROOT`; the startup guard must make clear that relative bind mounts
   cannot be guaranteed in this state.
4. **Peer path** (`use_peer` True): `resolved = _resolve_compose_host_paths(...)`;
   if `None`, log an error and return `False` **without** running direct compose.
   Else build `inner = _build_compose_up_cmd(app, resolved.compose_files,
   resolved.env_file)` and
   `result = _run_peer_compose(app, inner, resolved.bind_specs, detach=False, delay=0)`.
   If `result is None`, log an error and return `False` **without** running
   direct compose. Once a differing host path is known, direct execution is the
   broken mode this change is fixing and is not a safe fallback. Log
   stdout/stderr with the same `[compose/<app>]` prefix; return
   `result.returncode == 0`; handle `TimeoutExpired`/`FileNotFoundError` like
   the direct path.

Keep `run_compose`'s `bool` return contract identical.

### Step 3 — Self-update call site unchanged
Reconcile loop still calls `spawn_compose_helper()` for self (detached); regular
apps still call `run_compose()`. Only internals change.

### Step 4 — Startup guard (non-fatal), after `log_mounts()` (~1543)
Extract `_log_compose_path_mode()` so all branches can be unit-tested without
setting up a full reconciliation. It must never raise or block startup.
- Resolve `_host_root = _resolve_host_path(GITOPS_ROOT)` once.
- `None`: WARNING must name the current `AGENT_CONTAINER_NAME`, state that
  relative bind mounts cannot be guaranteed, and tell the operator to set the
  variable to the real container name.
- Different from `str(GITOPS_ROOT)`: INFO must show both paths and state that
  compose applies use the peer helper.
- Equal to `str(GITOPS_ROOT)`: DEBUG must state that direct compose is used.

---

## 4. Tests (`tests/test_steward.py`)
Mock `_container_mounts()`/`_resolve_host_path`, `subprocess.run`, `_get_helper_image`.
1. Peer path chosen when paths differ: assert `docker run --rm` (no `-d`) with
   `-v /home/u/git:/home/u/git`, the host workdir bind, `--entrypoint sh`, and
   `-f <host compose path>` in the `sh -c` string. Assert outer timeout `310`,
   no `-d`, and no container-internal path in the peer command.
2. Direct compatibility path when host root is unresolved
   (`_resolve_host_path`→`None`), with the startup warning covered separately.
3. Direct fast path when identical (host == container).
4. When paths differ and helper image is missing, `run_compose()` returns
   `False` and never invokes direct compose.
5. Override + env file propagate to the peer inner command. Include a path with
   spaces to prove each argv item is quoted exactly once.
6. Failure to resolve a present override or configured env file returns `False`
   and does not invoke either peer or direct compose.
7. A compose, override, or env file on a separate mount is passed in the command
   and bind-mounted read-only at the same host path.
8. Named-volume `Source` (`/var/lib/docker/volumes/x/_data`) is used as
   `host_root`; a more specific app-workdir mount is also preserved.
9. Self-update stays detached (`-d`, `sleep 5 &&`, outer timeout `30`) and keeps
   its explicitly separate direct-fallback behavior.
10. `_log_compose_path_mode()` logging (caplog): None→WARNING including the
    current `AGENT_CONTAINER_NAME`, differ→INFO, equal→DEBUG.
11. `run_compose` returns True on rc 0 / False otherwise in both paths, and
    logs peer stdout/stderr with the existing `[compose/<app>]` prefix.
Update any existing `run_compose` test assuming only the in-container form.
Pin those existing direct-command tests to the equal-path branch.

---

## 5. Docs
`README.md`: note that managed stacks may bind-mount repo files via **relative
paths**, resolved transparently (peer helper when container≠host path); mention
the startup guard + that `AGENT_CONTAINER_NAME` must match the real container
name. No change to `docker-compose.yml` / `.env.example`.

---

## 6. Verification (run all; fix before done)
1. `uv run ruff check steward.py metrics_server.py tests/`
2. `uv run ruff format --check steward.py metrics_server.py tests/`
3. `uv run pytest tests/ -v --tb=short`
Manual (node where container≠host path, e.g. infra-1):
4. Deploy new image; `sudo rm /git`; confirm the relative-bind-mount stack
   (strongswan `berlin/`) reconciles + starts **without** the symlink.
5. Confirm an absolute-path stack (sabnzbd-arr) still reconciles.
6. Startup logs show the guard INFO line.
7. Self-update still works.

---

## 7. Rollout
This repo has no separate version file to bump. After merge, create the next
SemVer `v*` tag. CI publishes the image and opens the `bump-self-image` PR that
updates the image pin in `docker-compose.yml`; merge that PR and confirm
self-update. Only then remove the `/git` symlink on each node.

## 8. Scope / non-goals
In: transparent peer-compose + startup guard + tests + README note.
Out: Option A mount-model change, rewriting stack compose files, manifest schema
changes, or a fatal startup mode. Failure to prepare a required peer apply is an
app reconcile failure, not a steward startup failure.