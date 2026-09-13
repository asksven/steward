# Plan: Fix relative bind-mount paths in managed stacks (Option B — transparent)

Status: **Steps 0-7 and §5 Docs complete.** Target repo: `asksven/steward`
(this repo). Executor: implement exactly as written; do not change the
deployment model (no migration). Follow the repo conventions in
`.github/copilot-instructions.md`.

All `steward.py:NNN` and `tests/test_steward.py:NNN` references below were
re-verified against the tree **after Steps 0-7 and §5 Docs landed**. Read the cited code
before editing it. If a reference looks off, `grep` for the symbol rather than
trusting the number — the file has already grown 1680 → 1960 lines during
implementation.

---

## 0. Decisions locked (do not re-litigate)

| # | Decision |
|---|---|
| D1 | An unresolvable **present override file** or **configured `compose_env_file`** is a **hard failure in both paths**, including self-update. Self-update's *existing* fallbacks for unresolvable `host_root` / workdir / main compose file, and for a missing helper image, are **preserved**. |
| D2 | Steward's process environment **is forwarded** to the peer as repeated `-e KEY=VALUE`, excluding `HOME` and every `DOCKER_*` key. `-e HOME=/tmp` is appended last. |
| D3 | The two latent mount-resolution bugs in `_find_best_mount()` / `_resolve_host_path()` are **fixed in this change**. |
| D4 | Peer command logging is **split by level**: callers log the *inner* `docker compose` argv at their own level (INFO in `run_compose`, matching the direct path); `_run_peer_compose()` keeps its own DEBUG line for the *outer* `docker run` wrapper. Both go through `_redact_peer_cmd()`. |

---

## 0a. Implementation status

| Step | State | Notes |
|---|---|---|
| 0 — Read the code | ✅ done | Orientation only. |
| 1 — Fix mount resolution (D3) | ✅ done | `_is_under()` steward.py:72; `_find_best_mount()` :82; `host_path()` :97; `_resolve_host_path()` :116. |
| 2 — Shared peer helpers | ✅ done | `_compose_files()` :864, `_compose_file_args()` :881, `PeerComposePaths` :890, `_PEER_FALLBACK_REASONS` :910, `_resolve_compose_host_paths()` :913, `_peer_env_args()` :982, `_redact_peer_cmd()` :1004, `_build_compose_up_cmd()` :1024, timeout constants :1041-1043, `_run_peer_compose()` :1046. |
| 3 — Refactor `spawn_compose_helper()` | ✅ done | See §3a for exactly what shipped; current function is steward.py:793-859. |
| 4 — Peer path in `run_compose()` | ✅ done | `run_compose()` delegates to `_run_compose_impl()` at steward.py:1214; direct mode remains the compatibility path. See §4a. |
| 5 — Startup guard | ✅ done | `_log_compose_path_mode()` steward.py:148; call after `log_mounts()` at :1823. |
| 6 — Call sites unchanged | ✅ done | Verified `sync_app()` at :1427-1430 and the self-heal ternary at :1619-1621; no edits required. |
| 7 — Review remediation | ✅ done | 7.1-7.7 implemented; 7.8 assessed and retained as a manual Compose verification gate. |
| §4 tests | ✅ done | Step 7 regression coverage is complete; 153 tests pass. |
| §5 docs | ✅ done | README updated with peer Compose behavior, mount-backed `compose_env_file` requirements, startup guard, and redacted peer logging. |

**Green baseline: 153 tests passing.** Any drop is a regression you introduced.

**Tooling — this sandbox has no `uv`, no `pip`, no `ensurepip`.** A gitignored
`.venv` is already bootstrapped with `requirements.txt` + `pytest` + `ruff`. Use
it directly:

```
.venv/bin/ruff check steward.py metrics_server.py tests/
.venv/bin/ruff format --check steward.py metrics_server.py tests/
.venv/bin/python -m pytest tests/ -q
```

`ruff format` reformats on write; run the `--check` variant last to confirm.

---

## 1. Problem

Steward-managed stacks whose compose file bind-mounts files **from the repo via
relative paths** (e.g. `./ipsec.secrets:/etc/ipsec.secrets:ro`) fail to start:

```
error mounting "/git/stacks/<app>/<sub>/ipsec.secrets" ... not a directory:
Are you trying to mount a directory onto a file
```

Root cause: `run_compose()` (steward.py:1097) runs `docker compose up` **inside**
the steward container using container-internal paths (`GITOPS_ROOT`, default
`/git`). A relative bind source like `./ipsec.secrets` resolves — inside steward
— to `/git/stacks/<app>/<sub>/ipsec.secrets`. That absolute path is handed to the
**host** Docker daemon, which has no `/git`, so it auto-creates empty
directories there and the file-mount fails. Absolute host-path bind mounts are
unaffected, which is why only repo-file-mounting stacks break.

## 2. Chosen approach (Option B, confirmed)

Steward **already** solves this for self-update. `spawn_compose_helper()`
(steward.py:793) resolves host paths via `_resolve_host_path()` (steward.py:116)
and runs a **peer container** with `-v {host_root}:{host_root}` and
`-f {host_compose_file}`. Inside that peer the host path is valid, so Compose
reads the file and resolves relative bind sources to host paths correctly.

**Generalize that mechanism to all apps.** `run_compose()` gains a
peer-execution path when the container path differs from the host path;
self-update keeps its detached peer. Fully transparent: no change to how steward
is mounted, no manifest schema change, no migration. The manual `/git` symlink
becomes unnecessary.

Do **not** implement Option A (changing the default mount / `GITOPS_ROOT`).

---

## 3. Implementation steps

### Step 0 — Read the current code ✅ DONE

> Line numbers in Steps 0-3 are **historical** (pre-Step-1 tree) and are kept
> only as a record of what was changed. Do not chase them. For current
> locations see the §0a table.

In `steward.py`: `_container_mounts()` (56), `_find_best_mount()` (72),
`host_path()` (86), `_resolve_host_path()` (102), `log_mounts()` (115),
`_is_self_update()` (727), `_get_helper_image()` (732),
`spawn_compose_helper()` (748), `_compose_file_args()` (857), `run_compose()`
(874), `_load_compose_services_status()` (945), `_load_expected_services()`
(1017).

The two `_is_self_update` branch points are now **steward.py:1427-1430**
(`sync_app`) and **steward.py:1619-1621** (`reconcile_app`, the self-heal
ternary). Both stay exactly as they are.

The startup block is in **`reconcile()` (steward.py:1789)** — there is no
`main()` in this file. The new guard goes immediately after `log_mounts()` at
**steward.py:1823**.

### Step 1 — Fix mount resolution (D3) ✅ DONE

Shipped as specified. Current locations: `_is_under()` steward.py:72,
`_find_best_mount()` :82, `host_path()` :97, `_resolve_host_path()` :116.
Tests at tests/test_steward.py:515, :529, :543, :556, :569, :583.

1. Add `_is_under(path: str, parent: str) -> bool`: `True` when
   `path == parent` or `path.startswith(parent.rstrip("/") + "/")`. Used by both
   the mount fix and bind-spec deduplication in Step 2.
2. `_find_best_mount()`: a mount's `Destination` matches only
   when `_is_under(str(container_path), dest)`. Previously a bare
   `startswith(dest)`, so a mount at `/opt/gitops` wrongly
   matched `/opt/gitopsdata/x.env` and yielded a plausible-but-wrong host path.
   Track `best_dest` (the matched string) rather than `best_len`, and compute
   `rel = str(container_path)[len(best_dest):].lstrip("/")` so the `dest == "/"`
   case still works.
3. `_resolve_host_path()`: return `None` when
   `best.get("Source")` is falsy. tmpfs and source-less mounts previously produced
   `"" + "/" + rel` — a bogus absolute path. D1's strictness is only sound if
   `None` is trustworthy.
4. `host_path()` (debug output only): when `Source` is falsy,
   return `"<no host source>"` plus the existing `[volume: <name>]` suffix
   instead of a misleading path.

### Step 2 — Shared peer helpers ✅ DONE

1. `PeerComposePaths` dataclass: `host_root: str`, `host_workdir: str`,
   `compose_files: list[str]` (raw host paths, no `-f` markers, no quoting),
   `env_file: Optional[str]` (host path), `bind_specs: list[str]`.

2. Split the existing `-f` builder so the direct and peer paths share one source
   of truth:
   - New `_compose_files(app, stack_path) -> list[str]` — returns
     `[compose_file]` plus `docker-compose.override.yml` when present, keeping
      the existing override DEBUG log (now steward.py:877).
   - `_compose_file_args()` (now steward.py:881) becomes a thin wrapper that
     interleaves `-f`. Its other two callers (now steward.py:1213 and
     steward.py:1285) stay untouched.

3. `_resolve_compose_host_paths(app, stack_path) -> tuple[Optional[PeerComposePaths], str]`

   The second element is `""` on success, else one of `"host_root"`,
   `"workdir"`, `"compose_file"`, `"override"`, `"env_file"`. Declare a
   module-level
   `_PEER_FALLBACK_REASONS = frozenset({"host_root", "workdir", "compose_file"})`
   so the D1 policy is explicit and unit-testable.

   Resolve in this order, returning `(None, reason)` on the first failure:
   - `GITOPS_ROOT` → `host_root`
   - the app workdir `stack_path / app.path` → `host_workdir`. Mounting both
     paths makes the compose project directory visible even when a nested, more
     specific mount covers it with a different host source.
   - the main compose file `stack_path / app.path / app.compose_file`
   - `docker-compose.override.yml` in the same directory, **if it exists**
   - if `app.env_file` is set: first require the **container** path to exist
      (mirroring the check at steward.py:1118, reason `"env_file"`), then resolve
     it.

   Never silently omit an override or env file and apply a changed stack
   definition.

   Build `bind_specs`, order-preserving and deduplicated by exact string:
   - `f"{host_root}:{host_root}"`
   - `f"{host_workdir}:{host_workdir}"` — nested binds are legal in Docker.
   - For every compose, override, or env file whose resolved host path is not
     `_is_under` either bound directory, add a read-only file bind at the same
     absolute path: `f"{p}:{p}:ro"`. This covers inputs supplied through
     separate or more-specific mounts.

4. `_peer_env_args() -> list[str]` (D2): for each `os.environ` item, skip `HOME`
   and any key starting with `DOCKER_`, else emit `["-e", f"{k}={v}"]`. Append
   `["-e", "HOME=/tmp"]` last so it wins (Docker: the last `-e` for a key takes
   precedence).

   Rationale: the direct path passes `env=os.environ.copy()` (steward.py:1133,
   :1143), so `${VAR}` interpolation in stack compose files currently resolves
   against steward's runtime environment. The Dockerfile declares no `ENV`, so
   without forwarding the peer would lose `GITOPS_NODE_NAME`, `STEWARD_UID` /
   `STEWARD_GID`, `HOSTNAME` and friends, silently substituting empty strings.
   `DOCKER_*` is excluded because `DOCKER_HOST` / `DOCKER_CONFIG` would redirect
   or break the peer's docker client, which must use the mounted socket.

5. `_redact_peer_cmd(cmd: list[str]) -> str` — join for display, but for the
   token immediately following any `-e`, emit only the substring before the
   first `=`. **Every** log statement that prints a peer command must use this.
   Without it, D2 leaks all forwarded values into the peer-command log lines,
   and README.md:341 actively tells operators to run `LOGLEVEL=DEBUG`.

   **As shipped (D4):** `_run_peer_compose()` owns one DEBUG line for the
   *outer* `docker run` argv — `log.debug("Peer compose command for app '%s': %s",
    app.name, _redact_peer_cmd(cmd))` at steward.py:1091. Callers do **not** see
   that argv (the function builds it internally and returns only
   `Optional[CompletedProcess]`), so callers log the *inner* `docker compose`
   argv at their own level instead. Do not change `_run_peer_compose()`'s
   signature or return type to expose the outer argv — four tests pin it.

6. `_build_compose_up_cmd(app, compose_files: list[str], env_file: Optional[str]) -> list[str]`

   → `["docker", "compose", "--project-name", app.name, *("-f", f)…,
   *(("--env-file", env_file) if env_file else ()), "up", "-d",
   "--remove-orphans", "--pull", app.pull_policy]`.

   Unquoted argv in, unquoted argv out. Used by **both** the direct and peer
   paths so flags cannot drift; `_run_peer_compose()` alone owns shell quoting.

7. `_run_peer_compose(app, inner_cmd, bind_specs, *, detach: bool, delay: int) -> Optional[subprocess.CompletedProcess]`

   Named constants — use these names, not "outer timeout":
   - `_PEER_INNER_TIMEOUT_S = 300` — the `timeout(1)` budget *inside* the peer.
   - `_PEER_SPAWN_TIMEOUT_S = 30` — `subprocess.run(timeout=…)` when `detach`.
   - `_PEER_RUN_TIMEOUT_S = 310` — `subprocess.run(timeout=…)` when not.

   Body:
   - `helper_image = _get_helper_image()`; if falsy, return `None`. **This is
     the only meaning of `None`: no peer image is available.**
   - `inner_shell = shlex.join(inner_cmd)` — guarantees each argv item is quoted
     exactly once.
   - `script = (f"sleep {delay} && " if detach and delay else "") +
     f"timeout {_PEER_INNER_TIMEOUT_S} {inner_shell}"`
   - `cmd = ["docker", "run", "--rm", *(["-d"] if detach else []),
     "--entrypoint", "sh", "-v", "/var/run/docker.sock:/var/run/docker.sock",
     *(("-v", b) for b in bind_specs), *_peer_env_args(), helper_image, "-c",
     script]` — `-c script` must stay **last**
      (tests/test_steward.py:2600 indexes `cmd[-1]`).
   - `subprocess.run(..., capture_output=True, text=True,
     timeout=_PEER_SPAWN_TIMEOUT_S if detach else _PEER_RUN_TIMEOUT_S)`.
   - Do **not** catch `subprocess.TimeoutExpired` or `FileNotFoundError`;
     callers keep their context-specific logging and `bool` return behavior.
     `spawn_compose_helper()`'s existing broad `except Exception`
      (now steward.py:859) already covers it and stays.

### Step 3 — Refactor `spawn_compose_helper()` ✅ DONE

1. `_get_helper_image()` falsy → keep the existing warning and
   `return run_compose(app, stack_path)` (pins
   `test_spawn_compose_helper_falls_back_when_helper_image_missing`).
2. `paths, reason = _resolve_compose_host_paths(app, stack_path)`
   - `reason in _PEER_FALLBACK_REASONS` → keep the existing "cannot resolve host
     paths" warning and fall back to `run_compose` (pins
     `test_spawn_compose_helper_falls_back_when_host_path_lookup_fails`).
   - `reason` truthy and **not** in that set (i.e. `"override"` / `"env_file"`)
     → `log.error` naming the file and `return False`. **This replaced the
     old warn-and-continue branches.** README.md:275 documents that steward's
     node-local override carries its SSH mounts and port bindings; restarting
     without it produces a steward that cannot sync and cannot self-heal, whereas
     failing loudly leaves the working image in place.
3. `inner = _build_compose_up_cmd(app, paths.compose_files, paths.env_file)`;
   `result = _run_peer_compose(app, inner, paths.bind_specs, detach=True, delay=5)`.
   Defensively, `result is None` → warn and fall back (unreachable given step 1).
4. Preserve the existing log messages (spawning / failed / launched / exception).
   Peer-command logging follows D4 — see §3a.

This fallback behavior is specific to the self-update restart model and must
**not** be copied to the regular-app peer path.

### 3a. What Step 3 actually shipped

`spawn_compose_helper()` is steward.py:793-859. Branch layout:

| Lines | Branch | Behavior |
|---|---|---|
| 809-815 | `not helper_image` | WARNING + `return run_compose(...)` |
| 817-823 | `reason in _PEER_FALLBACK_REASONS` | WARNING + `return run_compose(...)` |
| 824-829 | `reason == "override"` | `log.error` + `return False` |
| 830-835 | `reason == "env_file"` | `log.error` naming `app.env_file` + `return False` |
| 836-838 | `if reason:` catch-all | `log.error` with `reason=%s` + `return False` |
| 840 | success | `_build_compose_up_cmd(...)` |
| 842 | success | `log.info("Self-update: spawning helper container (image=%s)", ...)` |
| 845 | success | `_run_peer_compose(..., detach=True, delay=5)` inside the existing broad `try/except Exception` |
| 846-850 | `result is None` | defensive WARNING + `return run_compose(...)` |

`spawn_compose_helper()` deliberately logs **no** peer argv of its own: the
outer argv is covered by `_run_peer_compose()`'s DEBUG line (steward.py:1091),
and the inner argv adds nothing at INFO for a self-update (the existing
"spawning helper container" line already marks the event). Step 4's `run_compose`
**does** log the inner argv at INFO, because the direct path it replaces does
(steward.py:1135) and dropping it would be an observability regression.

Tests added: `test_spawn_compose_helper_hard_fails_on_unresolvable_override`
(tests/test_steward.py:1241), `..._on_unresolvable_env_file` (:1277),
`test_spawn_compose_helper_forwards_env_and_redacts_log` (:1313). The three
pre-existing tests (:1174, :1205, :2568) pass unmodified.

### Step 4 — Add the peer path to `run_compose()` ✅ DONE

1. Keep the `compose_file.exists()` check and the inside/outside DEBUG logging
   (steward.py:1102-1108).
2. Decide the mode:
   ```python
   host_root = _resolve_host_path(GITOPS_ROOT)
   use_peer = host_root is not None and host_root != str(GITOPS_ROOT)
   ```
3. **Direct path** (`use_peer` False): behavior identical to today — container
   `-f` paths and container `--env-file` (now built via `_compose_files` +
    `_build_compose_up_cmd`), `env=os.environ.copy()` (steward.py:1126),
    `cwd=stack_path / app.path`, `subprocess.run(timeout=300)` (:1153), the
    `env_file` existence check at steward.py:1118, the INFO command line at
    :1135, `[compose/<app>]` logging at :1164 / :1167, and the returncode /
    `TimeoutExpired` (:1155) / `FileNotFoundError` (:1158) handling. This is also
   the compatibility path when `docker inspect` cannot resolve `GITOPS_ROOT`;
   Step 5's guard makes clear that relative bind mounts cannot be guaranteed in
   that state.

   Switching this branch to `_build_compose_up_cmd` produces a byte-identical
   argv (same order: `--project-name`, `-f`…, `--env-file`?, `up -d
   --remove-orphans --pull`), so the four pinned direct-path tests keep passing.
4. **Peer path** (`use_peer` True):
   - `paths, reason = _resolve_compose_host_paths(app, stack_path)`; if `reason`,
     `log.error` naming the reason and `return False` **without** running direct
     compose. For regular apps **every** reason is fatal: once a differing host
     path is known, direct execution *is* the broken mode this change fixes and
     is not a safe fallback.
   - `result = _run_peer_compose(app, _build_compose_up_cmd(app,
     paths.compose_files, paths.env_file), paths.bind_specs, detach=False,
     delay=0)`; `result is None` → `log.error` and `return False`, again with no
     direct fallback.
   - **Logging (D4):** log the *inner* `docker compose` argv at INFO via
    `_redact_peer_cmd`, mirroring the direct path's line at steward.py:1135 —
     e.g. `log.info("Reconciling app '%s' via peer helper: %s", app.name,
     _redact_peer_cmd(inner))`. Do **not** try to log the outer `docker run`
     argv here; `_run_peer_compose()` already covers it at DEBUG
     (steward.py:1091) and does not return it. Then stdout at INFO and
     stderr at WARNING with the existing `[compose/<app>]` prefix; return
     `result.returncode == 0` with the same error message as the direct path;
     handle `TimeoutExpired` / `FileNotFoundError` identically to the direct
     path.
   - No `-w` / `--project-directory` is passed: Compose derives the project
     directory from the first `-f` file's own directory, which is already the
     host workdir. This is deliberate — do not add one.

`run_compose()`'s `bool` return contract is unchanged.

### 4a. What Step 4 actually shipped

`run_compose()` is steward.py:1097-1199. It keeps the compose-file existence
check and debug path logging, then selects the mode once:

```python
host_root = _resolve_host_path(GITOPS_ROOT)
use_peer = host_root is not None and host_root != str(GITOPS_ROOT)
```

Direct mode uses `_build_compose_up_cmd()` with container paths, preserves
`os.environ.copy()`, `cwd=stack_path / app.path`, timeout 300, and the existing
stdout/stderr/return-code handling. Unresolved `GITOPS_ROOT` therefore remains
the compatibility path.

Peer mode resolves `PeerComposePaths`, fails without a direct fallback on any
resolution error or missing helper image, invokes
`_run_peer_compose(..., detach=False, delay=0)`, and logs the inner compose argv
at INFO. The outer `docker run` argv remains logged once at DEBUG by
`_run_peer_compose()` and is redacted there. No working-directory flag was
added.

Step 4 tests are in tests/test_steward.py:1358-1901. The four existing direct
command tests now call `_pin_direct_compose()` so they genuinely exercise the
equal-path branch. The full suite baseline after Step 5 is 138 passing.

### Step 5 — Startup guard (non-fatal) ✅ DONE

Extract `_log_compose_path_mode()` so every branch is unit-testable without a
full reconciliation, and call it after `log_mounts()` (steward.py:1823, inside
`reconcile()` which starts at :1789). It must
never raise or block startup. Resolve `_resolve_host_path(GITOPS_ROOT)` once:
- `None` → WARNING naming the current `AGENT_CONTAINER_NAME`, stating that
  relative bind mounts cannot be guaranteed, and telling the operator to set
  that variable to the real container name.
- Different from `str(GITOPS_ROOT)` → INFO showing both paths and stating that
  compose applies use the peer helper.
- Equal to `str(GITOPS_ROOT)` → DEBUG stating that direct compose is used.

Implementation: `_log_compose_path_mode()` is steward.py:148-171. It catches
unexpected resolution/logging errors and emits a warning rather than allowing
the startup path to fail. Tests are tests/test_steward.py:591-650.

### Step 6 — Call sites unchanged (verify only) ✅ DONE

The reconcile loop still calls `spawn_compose_helper()` for self (detached) and
`run_compose()` for regular apps, at steward.py:1427-1430 (`sync_app`) and
steward.py:1619-1621 (the self-heal ternary in `reconcile_app`). Only internals
change — there is nothing to edit here, just confirm both sites still read as
before. Verified against the current tree; `git diff` contains no changes to
either call site.

### Step 7 — Post-review remediation (blockers + should-fix) ⬅ NEXT TASK

The Steps 0-6 review found three release blockers and associated should-fix
coverage and diagnostic items.
Implement this step **before README changes, manual deployment, or rollout**.
The current green baseline is 146 tests; that does not prove the items below —
the first two blockers were reproduced against the current implementation.

#### 7.1 — BLOCKER: make self-update fallbacks genuinely direct ✅ DONE

Current bug: `spawn_compose_helper()` claims to fall back to direct compose at
steward.py:815, :823, and :850, but called public `run_compose()`. That function
recalculated peer mode at :1119-1120. If the root host path differs, the
fallback re-enters the peer path and fails again. Missing helper image was
reproduced as: WARNING "falling back to direct compose" → INFO "via peer helper"
→ `False`.

Refactor without changing public `run_compose(app, stack_path) -> bool` or the
two Step 6 call sites:

1. Move the current `run_compose()` body into a private implementation with an
   explicit mode override, e.g.
   `_run_compose_impl(app, stack_path, *, force_direct: bool) -> bool`.
2. Keep public `run_compose(app, stack_path)` as a thin wrapper calling the
   implementation with `force_direct=False`.
3. Add `_run_compose_direct(app, stack_path) -> bool` as a thin private wrapper
   calling the same implementation with `force_direct=True`.
4. Change **only** self-update fallback branches at steward.py:815, :823, and
   :850 to call `_run_compose_direct()`. Preserve their current warning text.
5. `force_direct=True` must bypass peer-mode selection but retain all normal
   direct-path validation and behavior: compose-file and env-file existence
   checks, `_build_compose_up_cmd()` with container paths, `os.environ.copy()`,
   `cwd=stack_path / app.path`, timeout 300, output logging, and bool return.
6. Keep D1 strictness unchanged: an unresolvable present override or configured
   env file in self-update still returns `False`; regular-app peer failures and
   missing peer images still have **no** direct fallback.

Do not solve this by having `spawn_compose_helper()` duplicate the direct
subprocess body, and do not add a public optional argument to `run_compose()`.

Acceptance tests:

- Missing helper image while `GITOPS_ROOT` resolves to a **different** host path
  executes an actual `docker compose` argv, not `docker run`, and returns that
  direct execution's bool result.
- Parameterize self-update resolution failures for `"host_root"`, `"workdir"`,
  and `"compose_file"`; each executes direct compose exactly once and never
  attempts a peer run.
- Preserve the existing strict override/env-file tests and regular-app
  no-fallback tests.
- Replace/strengthen `test_spawn_compose_helper_falls_back_when_helper_image_missing`
   (tests/test_steward.py:1171) and
   `...falls_back_when_host_path_lookup_fails` (:1200): their current
  `run_compose = lambda: True` mocks hide this blocker and are insufficient.

#### 7.2 — BLOCKER: prevent environment leakage on self-update timeout/errors ✅ DONE

Current bug: `_run_peer_compose()` puts forwarded `-e KEY=VALUE` tokens in the
argv at steward.py:1075-1088 and propagates `TimeoutExpired`. The broad handler
at steward.py:859-861 logged the exception value. Python's `TimeoutExpired`
message embeds the complete argv, leaking every forwarded environment value at
ERROR level. This was reproduced with `SECRET_TOKEN=top-secret` appearing in
the log.

1. In `spawn_compose_helper()`, catch `subprocess.TimeoutExpired` separately and
   emit a fixed error such as `Self-update: helper launch timed out`; do not log
   the exception object, `e.cmd`, or any peer argv.
2. Catch `FileNotFoundError` separately with a fixed message indicating Docker
   is unavailable; do not log the exception value.
3. Retain a final generic `except Exception`, but log only the exception class
   name (e.g. `type(exc).__name__`), not `str(exc)`. An arbitrary exception from
   a subprocess wrapper can also carry the command.
4. Keep `_run_peer_compose()`'s propagation contract unchanged; callers own
   context-specific handling.
5. Do not weaken normal DEBUG observability: the outer peer command still goes
   through `_redact_peer_cmd()` at steward.py:1091.

Acceptance tests:

- Set a sentinel secret in `os.environ`, make detached peer execution raise
  `TimeoutExpired(cmd=<actual peer argv>, timeout=30)`, capture all log levels,
  and assert the sentinel is absent from every record while the fixed timeout
  message is present.
- Add equivalent fixed-message coverage for `FileNotFoundError`.
- Add one generic-exception test whose message contains a sentinel secret;
  assert only the exception class, not the message/secret, is logged.

#### 7.3 — BLOCKER: account for more-specific app-workdir mounts in mode selection ✅ DONE

Current bug: `run_compose()` chose peer mode using only `GITOPS_ROOT` at
steward.py:1119-1120. This broke the target behavior when root paths were
identical but an app has a more-specific mount:

```text
container /git             -> host /git
container /git/stacks/demo -> host /srv/demo
```

Direct compose resolves `./config` to `/git/stacks/demo/config` for the host
daemon, but the real source is `/srv/demo/config`.

Inside `_run_compose_impl()` from 7.1, decide mode as follows:

```python
host_root = _resolve_host_path(GITOPS_ROOT)
container_workdir = stack_path / app.path
host_workdir = _resolve_host_path(container_workdir) if host_root is not None else None

if force_direct or host_root is None:
    use_peer = False
else:
    use_peer = (
        host_root != str(GITOPS_ROOT)
        or host_workdir != str(container_workdir)
    )
```

Important consequences:

- `host_root is None` remains the documented direct compatibility path.
- If root paths match but `host_workdir is None` (e.g. a source-less nested
  mount), select peer mode; `_resolve_compose_host_paths()` then fails cleanly
  rather than applying with a wrong host path.
- Do not use only the compose-file mapping; app workdir is the Compose project
  directory and therefore controls relative bind resolution.
- `_resolve_compose_host_paths()` may resolve the workdir again. Avoiding one
  Docker inspect is not worth weakening correctness; optimize only if the
  resulting code stays straightforward.

Update `_log_compose_path_mode()`'s equal-root DEBUG message at steward.py:170.
It must no longer claim all applies use direct compose. Use wording such as:
`Compose path mode: root paths match; direct compose is used unless an app has
a more-specific mount`.

Acceptance tests:

- Root host path equals container root, but host workdir differs: assert
  `run_compose()` uses foreground peer mode and host workdir/compose paths.
- Root and workdir paths both match: assert direct mode.
- Root matches but workdir resolution is `None`: assert failure without direct
  compose, not an unsafe compatibility fallback.
- Update the startup DEBUG assertion to match the qualified wording.

**7.1-7.3 shipped:** `_run_compose_impl()` is steward.py:1106, with public
`run_compose()` at :1214 and `_run_compose_direct()` at :1219. Self-update
fallbacks now call the private direct wrapper, and timeout/FileNotFound/generic
handlers at :860-866 do not log exception contents. Mode selection resolves the
app workdir at :1121-1127, and the guard uses the qualified root-match message
at :170-172. Regression tests are at tests/test_steward.py:1171-1380 and
:1610-1719. The full suite is 146 passing.

#### 7.4 — SHOULD FIX: cover successful peer override propagation ✅ DONE

Current tests cover direct overrides, command-building with a manually supplied
override list, and an unresolvable override. None proves the successful resolver
path at steward.py:947-952 reaches the peer command.

Add an end-to-end `run_compose()` peer test with a present, resolvable
`docker-compose.override.yml`:

- Main compose and override appear as two ordered `-f` arguments in the inner
  shell command.
- Put the override on a separate simulated mount and assert the outer
  `docker run` argv contains `{host_override}:{host_override}:ro` exactly once.
- Keep the main compose under host workdir and assert it receives no redundant
  read-only file bind.

#### 7.5 — SHOULD FIX: cover `run_compose()` exception contracts ✅ DONE

Add parameterized tests for both direct and peer modes:

- `subprocess.TimeoutExpired` → `False` and the app-specific timeout message.
- `FileNotFoundError` → `False` and the fixed Docker-not-found message.
- For peer mode, raise through `_run_peer_compose()` rather than bypassing the
  caller's exception handler.

The existing test at tests/test_steward.py:1149 proves only that
`_run_peer_compose()` propagates timeout; it does not test `run_compose()`.

#### 7.6 — SHOULD FIX: protect startup and self-update dispatch wiring ✅ DONE

Add wiring assertions so deleting one of these calls fails tests:

- In an existing lightweight `reconcile()` test, monkeypatch
  `_log_compose_path_mode()` and assert the call at steward.py:1823 occurs once.
- Add a `sync_app()` self-update test (`_is_self_update=True`) asserting
  `spawn_compose_helper()` is called and `run_compose()` is not.
- Extend an existing synced-drift/self-heal test for a self-update app to assert
  the branch at steward.py:1619-1621 chooses `spawn_compose_helper()` and not
  `run_compose()`.

Do not change the production call sites; Step 6 verified they are correct.

#### 7.7 — SHOULD FIX: correct the unresolved-container warning ✅ DONE

Current wording at steward.py:157-160 formats the current (possibly wrong) name
as if it were the value to set:

```text
set AGENT_CONTAINER_NAME='wrong-name' to the real container name
```

Change it to state the current value separately, e.g.:

```text
AGENT_CONTAINER_NAME is currently 'wrong-name'; set AGENT_CONTAINER_NAME to the real container name
```

Update the existing caplog test to require both the current name and the clear
instruction, while retaining the required "relative bind mounts cannot be
guaranteed" text.

#### 7.8 — SHOULD FIX / verification gap: exercise a real relative bind definition ✅ ASSESSED

The current tests use `services: {}` and mocked subprocesses. They prove argv
construction, not Docker Compose's project-directory behavior.

- If Docker Compose is available in CI without requiring a daemon, add an
  integration test using a compose file with
  `./ipsec.secrets:/etc/ipsec.secrets:ro` and verify Compose resolves the source
  against the host project directory supplied by the first `-f` path.
- If that cannot run reliably in CI, do **not** add a skipped/flaky test. Keep
  the command-shape tests and preserve the manual verification in §6 as a
  mandatory rollout gate. Record in the final implementation summary that the
  real Compose resolution remains manually verified.

Docker Compose is installed in this environment, but the Docker daemon is not
available (`/var/run/docker.sock` is absent). No skipped or daemon-dependent
integration test was added. The manual relative-bind verification in §6 remains
mandatory.

#### Step 7 completion gate

Before marking Step 7 complete:

1. All three blockers above are fixed and each reproduction is now a regression
   test.
2. All deterministic should-fix unit/wiring tests (7.4-7.7) are implemented.
3. Existing D1-D4 behavior remains intact.
4. `run_compose(app, stack_path) -> bool` and the two Step 6 production call
   sites remain unchanged externally.
5. Ruff and the complete test suite pass; the count must be **greater than 138**.
6. Update this status table and test inventory with the new baseline and current
   line references.

**Step 7 result:** 7.1-7.7 are implemented with regression tests; 7.8 is
assessed per the no-daemon rule above. The current full-suite baseline is 153.

---

## 4. Tests (`tests/test_steward.py`) ✅ DONE FOR STEPS 1-7

Mock `_container_mounts()` / `_resolve_host_path`, `subprocess.run`, and
`_get_helper_image`. The test helpers `_demo_app(**overrides)` and
`_stub_resolve_host_path(mapping)` are available for peer-path tests.

All deterministic Step 1-7 plan items are now covered:

- Mount resolution items 1-2: tests/test_steward.py:515-583.
- Peer selection and command shape items 3-9: tests/test_steward.py:1591,
  :1638, :1676, :1724, :1780, :1829, :1992, plus resolver tests at :893 and
  :925. The spaced-path test verifies `shlex.join` quoting exactly once.
- Strictness items 10-13: tests/test_steward.py:2022, :2062, :2095, and
  the self-update tests at :1199/:1258.
- Environment and redaction items 14-15: tests/test_steward.py:949/:973/:995,
  plus the end-to-end self-update/error tests at :1334/:1408.
- Return/logging contract item 17: tests/test_steward.py:2130 and :2174.
- Startup guard item 16: tests/test_steward.py:591-650, including the
  non-raising resolution-failure case.

The four existing direct-path command tests are explicitly pinned to the
equal-path branch using `_pin_direct_compose()`:

- `test_run_compose_uses_explicit_project_name` (:1445)
- `test_run_compose_uses_manifest_pull_policy` (:1480)
- `test_run_compose_includes_override_file_when_present` (:1516)
- `test_run_compose_omits_override_file_when_missing` (:1555)

This prevents them from passing accidentally through the unresolved-root
compatibility path when their global `subprocess.run` fake intercepts Docker
inspect. `test_spawn_compose_helper_uses_explicit_project_name` (:2875) remains
unchanged and still asserts on `cmd[-1]` (:2907), `--entrypoint sh`,
`--project-name steward`, and `timeout 300`.

Steps 1-7 plan tests are complete. Step 7 regression coverage includes:

- Genuine direct self-update fallback for missing image and all three fallback
  resolution reasons.
- Timeout/FileNotFound/generic-exception secret-safe logging.
- Root-equal/workdir-different nested mount selection and source-less workdir
  failure.
- Successful peer override propagation with a separate read-only bind.
- Direct and peer `run_compose()` timeout/FileNotFound contracts.
- Startup guard call wiring and both self-update dispatch call sites.
- Corrected unresolved-container warning wording.
- Real Compose relative-bind integration was assessed but not added because the
  available Docker daemon/socket is unavailable; manual verification remains
  mandatory.

Key Step 7 tests include `test_run_compose_peer_propagates_resolvable_override`,
`test_run_compose_direct_handles_subprocess_errors`,
`test_run_compose_peer_handles_subprocess_errors`,
`test_reconcile_calls_compose_path_mode`,
`test_sync_app_self_update_uses_spawn_helper`, and the strengthened
`test_reconcile_app_synced_drift_auto_self_heals`.

---

## 5. Docs (`README.md`) ✅ DONE

1. §"Self-update" / "Why a helper container?" (README.md:252-278): generalize
   the description — the peer helper now runs **every** compose apply whenever
   the container path differs from the host path, which is what makes relative
   repo-file bind mounts work. Note the startup guard and that
   `AGENT_CONTAINER_NAME` must match the real container name.
2. App manifest schema note (README.md:190-202) and §"Configuration":
   `compose_env_file` must now be on a path that is **bind-mounted into
   steward** (so it has a host equivalent), not merely readable inside the
   container — and an unresolvable one is a reconcile failure, not a warning.
   examples.yml:52 ships `/opt/gitops/arr.env`, which is fine only if that
   directory is mounted into steward.
3. §"Logs" (README.md:333-342): mention the new startup mode line, and that peer
   commands log `-e` key names only.

No change to `docker-compose.yml` or `.env.example`.

All three documentation items are implemented. The plan is now complete through
§5 Docs; only the manual rollout and release steps remain.

---

## 6. Verification (run all; fix before done)

There is no `uv` / `pip` / `ensurepip` in this sandbox. Use the pre-bootstrapped
gitignored `.venv`:

1. `.venv/bin/ruff check steward.py metrics_server.py tests/`
2. `.venv/bin/ruff format steward.py metrics_server.py tests/` then
   `.venv/bin/ruff format --check steward.py metrics_server.py tests/`
3. `.venv/bin/python -m pytest tests/ -q` — **must be ≥ 146 passing** (the
   Step 0-6 + 7.1-7.3 baseline). Use `-v --tb=short` when something fails.

Manual, on a node where container ≠ host path (e.g. `infra-1`):

4. Deploy the new image; `sudo rm /git`; confirm the relative-bind-mount stack
   (strongswan `berlin/`) reconciles and starts **without** the symlink.
5. Confirm an absolute-path stack (`sabnzbd-arr`) still reconciles.
6. Startup logs show the guard INFO line with both paths.
7. Grep the reconcile log at `LOGLEVEL=DEBUG` and confirm **no** forwarded env
   value appears.
8. Self-update still works.

---

## 8. Rollout

This repo has no separate version file to bump. After merge, create the next
SemVer `v*` tag. CI publishes the image and the `bump-self-image` job
(.github/workflows/build.yml:113) opens the PR updating the image pin in
`docker-compose.yml`; merge that PR and confirm self-update. Only then remove
the `/git` symlink on each node.

---

## 9. Scope / non-goals

**In:** transparent peer compose for all apps; D1 strictness; D2 env forwarding
with redacted logging; D3 mount-resolution fixes; startup guard; tests; README
updates.

**Out:** Option A mount-model change; rewriting stack compose files; manifest
schema changes; a fatal startup mode. Failure to prepare a required peer apply
is an app reconcile failure, not a steward startup failure.

**Deliberately unchanged:** `_load_compose_services_status()` (steward.py:1201)
and `_load_expected_services()` (steward.py:1273) keep running in-container with
container paths, and both still call `_compose_file_args()` (:881), which now
delegates to `_compose_files()` (:864) with identical output. `compose ps`
matches by project-name label, and `config --services` needs no host paths. D2
keeps this safe: because the peer now interpolates from the same environment as
the in-container `config --services`, expected-vs-live comparison stays
consistent and drift detection will not false-positive.

---

## 10. Verified context (so you don't have to re-check)

- D2 is a **no-op for self-update in the documented configuration**.
  `AGENT_IMAGE` and `STEWARD_DATA_DIR` are not in steward's container
  environment (docker-compose.yml:9-31), so they keep coming from the deployment
  `.env` via project-directory auto-load; the keys that *are* forwarded carry
  identical values.
- `os.environ` does retain the container environment under cron, since busybox
  `crond` passes its own environment to children (entrypoint.sh:113-125).
- No `DOCKER_HOST` / `DOCKER_CONFIG` / `DOCKER_TLS_*` reference exists anywhere
  in the repo, so the D2 exclusion is purely defensive.
- The Dockerfile declares no `ENV` lines, which is why the peer would otherwise
  start with only the `python:3.14-alpine` base environment.
