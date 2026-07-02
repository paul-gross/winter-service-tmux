# Workflow setup walkthrough

This guide is an interactive walkthrough that produces `workspace:/.winter/config/winter-service-tmux/config.toml` and `layout-hook.sh` in the same directory — the declarative manifest and layout hook that configure the service orchestrator (`./up`, `./down`, `./status`, `./restart`) for every feature worktree in the workspace. The manifest defines what services run, how they start, and which tmux panes they occupy. The layout hook creates those panes.

Run it on a fresh workspace, or any time you want to (re)configure how services are launched — add new services, rename panes, switch which env file gets sourced.

**Idempotent:** safe to re-run at any time. Before each step, check the current state of `workspace:/.winter/config/winter-service-tmux/config.toml`. If the step is already done, **say so explicitly** ("services are already wired — skipping") and move on. Don't silent skip.

## Caller contract

This guide is invoked by the `ws-setup` service-orchestration sub-guide (`workspace:/context/setup-service-orchestration.md`, Step 4) after it has already discovered and assigned services. The caller provides:

- **The set of services assigned to `winter-service-tmux`**, each with a **scope**: `"project"` (per-env) or `"workspace"`.
- **Whatever wiring facts were already discovered upstream** for each service, in the schema defined by `workspace:/context/service-discovery.md` — `start_command` and `port`, when the project's own evidence revealed them. Treat these as given; a field the caller didn't supply is the only kind you derive or ask about. That schema does **not** include a health/readiness signal — the health probe below is always this guide's own to derive or ask about, never caller-supplied.

This guide does not re-discover or re-assign services, and does not re-derive wiring facts the caller already supplied. It collects the remaining tmux-specific wiring (pane targets, layout, health probes, and any fields the caller left unresolved) for the assigned set and writes `config.toml` and `layout-hook.sh`.

## How to run this guide

This is a guided walkthrough, not a script. Your job is to teach the user how their service orchestration is wired together while configuring it. Be verbose, be explicit, and be patient.

**Follow the pacing rules of the setup process you're engaged in.** This guide is normally entered mid-walkthrough from `/ws-setup`, so honor that walkthrough's pacing throughout — one question per turn, speak before acting, narrate actions, don't pause between steps, and show what you found rather than silent-skipping.

## Why this matters

When an agent spins up a feature environment, it runs `./up` to start all the services needed to use the application. `config.toml` tells the orchestrator exactly what to launch and in which tmux pane. Without it, `./up` errors out in every worktree.

This extension does **not** own the environment. Winter-cli core computes each scope's environment and injects it into the provider process; the injected variable list and the per-action injection matrix are owned by `workspace:/context/winter-cli/contracts/service-orchestrator.md`. On `restart` — an action that does not receive a fresh provider-level injection — the relaunched pane self-sources its env via `eval "$(winter env <scope>)"`, the same mechanism every pane uses on `up`. Each pane's launch line includes this self-source prefix so panes always carry the correct scope env regardless of which action started them; no `.winter.env` file is written or sourced by core. When you run services directly from an env directory (e.g. `alpha/up`), the entry shim sources `eval "$(winter env <env>)"` before launching the orchestrator, so the process environment is already populated. The `env_file` key in `config.toml` is optional: set it only when a service command or health probe needs additional vars from a local file (e.g. machine-specific secrets) beyond what core injects.

## Prerequisites

Before running this guide:
- The workspace has been set up via `/ws-setup` (or the underlying `winter ws init` has been run).
- The `winter-service-tmux` extension is installed (its standalone clone exists and its `on_env_init` hook is wired into the workspace).
- Read `winter-service-tmux:/workflow/config.toml.example` to understand the manifest schema you'll be writing.
- Read `winter-service-tmux:/workflow/layout-hook.sh.example` to understand the layout hook contract.

## Opening preamble (always send first)

Before doing anything, send a short orientation message, then continue straight into the first step:

> "I'll walk you through setting up `config.toml` and `layout-hook.sh` in `.winter/config/winter-service-tmux/` so `./up`, `./down`, and `./status` know which services to run in each feature worktree. Stop me or ask questions at any time."

Don't wait for a "go" signal — just begin.

## Steps

### 1. Check existing config.toml

**Explain first:** "Before changing anything, I need to know what's already there. `workspace:/.winter/config/winter-service-tmux/config.toml` is the canonical source of truth — if it exists, it tells me your current `env_file` and services."

Check the current state:

```bash
if [[ -f ./.winter/config/winter-service-tmux/config.toml ]]; then
  cat ./.winter/config/winter-service-tmux/config.toml
else
  echo "(no config.toml)"
fi
```

**If a config already exists**, parse out and report what you found:

> "Your `config.toml` already exists: `env_file = "<value or 'unset'>"`, `<n>` services declared (`<name-list>`). Want to keep it as-is, replace it from scratch, or tweak something specific?"

- "keep": skip ahead to the "Validate" step and run the validator against the existing file — it's idempotent. Then offer the smoke test and continue into the final report.
- "replace": continue from the next step as if no file existed.
- "tweak": ask **"What would you like to change?"** and skip to the relevant step.

**If `config.toml` does not exist**, tell the user: "No `config.toml` yet — let's build one from scratch." Then continue.

### 2. Wire per-env services

`<prefix>` is `WINTER_SERVICE_PREFIX` — the workspace's `service_prefix`, resolved by winter-cli core and injected on every dispatched action (`up`/`down`/`status`/`restart`/`logs`). This guide does not configure the prefix; it's controlled entirely by the workspace, not per-provider.

**Explain first:** "Now I'll wire the per-env services (scope `"project"`) into `config.toml` and write the layout hook. Per-env services run in a separate tmux session per feature env (`<prefix>-<env>`). Panes are addressed as `<window>.<pane>` (both zero-based); these `target` values in the manifest must exactly match the windows and panes the layout hook creates."

If there are **no** per-env services in the assigned set, tell the user "No per-env services assigned — skipping to workspace services." and move on.

Skip any service already declared in `config.toml` with a matching `[[service]]` entry — show what you found.

**Start from what the caller supplied; only resolve what's missing — don't interrogate the user field by field.** For each remaining per-env service, use the wiring facts the caller already gave you (`cmd` from the schema's `start_command`, and `port`) wherever present — see the caller contract above for the full schema. For any field not supplied, infer it using the evidence sources `workspace:/context/service-discovery.md` defines for those fields (`package.json` scripts, `Procfile`, framework entry points, README) plus env-var declarations for anything the schema doesn't cover. Resolve:

- **cmd** — the start command. Use the caller-supplied command if given. A service at the worktree root is just its command (`cmd = "npm run dev"`); a service in a subdirectory prepends a relative `cd` (`cmd = "cd apps/backend && npm run dev"`). Keep the `cd` relative — the orchestrator resets each pane to the worktree root before running, so the same command works on both `./up` and `./restart`.
- **target** — the pane address (`<window>.<pane>`, both zero-based). Always yours to pick — pane layout is tmux-specific and never supplied by the caller. Pick a layout that keeps related services together (e.g. backend + frontend in window 0, a shell pane in window 1). Every target must be unique across per-env services.
- **port** — the port the service binds. Use the caller-supplied port if given. Two forms: `port = 5432` for a fixed absolute port (the same in every env); `port = "WINTER_PORT_BASE + <offset>"` for a per-env isolated port resolved at status time from the env's `WINTER_PORT_BASE`. Omit when the service declares no port.
- **health probe** — always yours to derive; never caller-supplied (see the caller contract above). `type = "url"` with an HTTP health URL (passes on 2xx/3xx) or `type = "cmd"` with a shell command that exits 0 when ready. `${VAR}` placeholders in the `target` string resolve from the injected env (core WINTER_* vars and the scope's env-var band entries); bare `$VAR` is not interpolated. Default timeout is 5 seconds; omit `timeout` unless non-default. Omit the probe when the service has no observable readiness signal.
- **startup retry** — `retries` (max re-launch attempts after the first failure) and optional `retry_delay` (seconds between attempts, default 2). Include only for services likely to fail transiently on boot. This policy is honored by `winter service up`; the env-root `./up` symlink does not honor it. Always yours to decide — not something the caller supplies.
- **env_file** — a path relative to the worktree root if any service command or health probe needs vars beyond core injection (WINTER_* base vars and the scope's env-var band entries are auto-injected; no env_file is needed for those). Omit if not needed.

If a field genuinely cannot be resolved from either the caller's facts or the project (e.g. a bespoke start command with no evidence anywhere), ask the user specifically about that one thing before presenting the proposal.

Also suggest a `shell` pane (empty `cmd = ""`) if the service set doesn't already include one: **"Add a `shell` pane for ad-hoc commands?"**

Then **present the full proposed wiring** and ask **one** question:

**"Here's how I'll wire your per-env services — for each: pane target, `cmd`, port (if any), health probe (if any), and startup policy (if any); plus `env_file` if needed and the layout the hook will create. Confirm, or tell me what to change?"**

- "confirm": apply (below).
- changes: fold in the user's corrections and re-present the proposal until they confirm.

On confirmation, **update `config.toml` and write `layout-hook.sh`**, following the schema in `winter-service-tmux:/workflow/config.toml.example` and the contract in `winter-service-tmux:/workflow/layout-hook.sh.example`. Winter-specific rules the schema docs assume:

- `layout_hook = "layout-hook.sh"` — bare filename, resolved relative to the config dir; place the hook alongside `config.toml`.
- `[service.health]` and `[service.startup]` subtables must be placed immediately after their parent `[[service]]`, before the next `[[service]]`.
- `${VAR}` placeholders in health `target` strings resolve from the injected env; bare `$VAR` is not interpolated.
- The layout hook must only create windows and panes — do not `tmux send-keys`, source env files, or `cd` in the hook; the orchestrator handles all of that. After writing, mark it executable: `chmod +x ./.winter/config/winter-service-tmux/layout-hook.sh`.

**Machine-specific overrides (mention, don't prompt):** tell the user: "If you ever need machine-specific overrides, drop a gitignored `config.local.toml` next to `config.toml` and it'll be merged on top." Create it only if explicitly asked.

Then summarise: "Per-env services wired: `<name-list>`. `config.toml` and `layout-hook.sh` written."

### 3. Wire workspace singleton services

**Explain first:** "Workspace-scoped services (scope `"workspace"`) run once under `<prefix>-workspace` at the workspace root, shared across all feature envs. Their start commands must run in the foreground — the orchestrator reaps the tmux session to shut them down, so commands that daemonise or detach won't be reaped cleanly."

If there are **no** workspace-scoped services in the assigned set, tell the user: "No workspace-level services were assigned to this orchestrator — skipping." and continue.

Skip any service already declared with `scope = "workspace"` in `config.toml` — show what you found.

**Start from what the caller supplied; only resolve what's missing.** Use the caller-supplied start command for each remaining workspace-scoped service if given; otherwise infer from the project its **blocking foreground start command** (e.g. `postgres -D /usr/local/var/postgres`, `rabbitmq-server`, `docker run -p 5432:5432 postgres:16`). Its **pane target** (`<window>.<pane>`) is always yours to pick — pane layout is tmux-specific and never supplied by the caller. Workspace pane targets are independent of per-env targets — the same address may appear in both scopes without conflict (they live in different tmux sessions).

Then **present the full proposed wiring** and ask **one** question:

**"Here's how I'll wire your workspace singletons — for each: pane target, `cmd`, and `scope = "workspace"`. Confirm, or tell me what to change?"**

- "confirm": apply.
- changes: fold in the user's corrections and re-present until they confirm.

On confirmation, **update `config.toml` and write `workspace-layout-hook.sh`**, following the schema in `winter-service-tmux:/workflow/config.toml.example` and the contract in `winter-service-tmux:/workflow/layout-hook.sh.example`. Winter-specific rules to honor: each workspace service entry needs `scope = "workspace"`; set `workspace_layout_hook = "workspace-layout-hook.sh"` (bare filename, resolved relative to the config dir); the workspace hook follows the same layout-only contract as `layout-hook.sh` but the orchestrator supplies `WINTER_ENV=workspace` and `WINTER_TMUX_WORKTREE_DIR=<workspace-root>` — `WINTER_ENV_INDEX` and `WINTER_PORT_BASE` are **not** set for the workspace hook. After writing, mark it executable: `chmod +x ./.winter/config/winter-service-tmux/workspace-layout-hook.sh`.

**Note on shutdown:** `winter service down workspace` reaps the tmux session and kills each pane's process tree — best-effort. A service that forks to the background or detaches from the pane's process group may survive. Run commands in the foreground so the session reap reaches them.

Then summarise: "Workspace singletons wired: `<name-list>`. Drive them with `winter service up/down workspace`."

### 4. Validate

**Explain first:** "Before testing live, validate the manifest — the validator catches schema errors, duplicate targets, and missing-service issues before they reach a running tmux session."

Run the validator from the extension worktree (substituting the actual worktree path):

```bash
cd alpha/winter-service-tmux
PYTHONPATH=src python3 -m service_manifest.cli validate ../../
```

The validator resolves the config dir via `WINTER_EXT_CONFIG_DIR` when set, otherwise falls back to `<workspace-root>/.winter/config/winter-service-tmux/`.

If the validator reports errors, fix them in `config.toml` (or `layout-hook.sh` if the issue is an unreachable `layout_hook` path) before continuing.

Confirm: "Manifest validates cleanly."

### 5. Smoke test (optional)

**Explain first:** "Before declaring done, you can verify the full lifecycle in a real worktree."

Ask **one** question:

**"Run a smoke test in `alpha/` now (`cd alpha && ./up && ./status && ./down`), or skip?"**

- "skip" / "no": continue.
- "run" / "yes": tell the user "Running `./up` in `alpha/`..." then run it. After it returns, run `./status` and report the pane states. If any pane shows an error, tell the user exactly what failed and offer to revisit the relevant step. After confirming services look healthy, run `./down` to tear the session down cleanly.

### Final report

Summarise everything that happened in a single message:
- `config.toml` location: `workspace:/.winter/config/winter-service-tmux/config.toml` (created / replaced / unchanged)
- `env_file` (value or "unset")
- Services declared (names and targets)
- `layout-hook.sh`: `workspace:/.winter/config/winter-service-tmux/layout-hook.sh` (written / unchanged)
- Workspace services declared (names and targets, or "none")
- `workspace-layout-hook.sh`: `workspace:/.winter/config/winter-service-tmux/workspace-layout-hook.sh` (written / skipped / unchanged)
- Validation: passed / errors (if errors, what to fix)
- Smoke test: ran / skipped / failed (if failed, what to fix)
- Any manual steps still pending

End with:

> "Workflow setup complete. You can re-run this guide any time — it's idempotent and will only apply changes that are still needed."
