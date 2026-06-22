# Workflow setup walkthrough

This guide is an interactive walkthrough that produces `workspace:/.winter/config/winter-service-tmux/config.toml` and `layout-hook.sh` in the same directory — the declarative manifest and layout hook that configure the service orchestrator (`./up`, `./down`, `./status`, `./restart`) for every feature worktree in the workspace. The manifest defines what services run, how they start, and which tmux panes they occupy. The layout hook creates those panes.

Run it on a fresh workspace, or any time you want to (re)configure how services are launched — add new services, rename panes, change the session prefix, switch which env file gets sourced.

**Idempotent:** safe to re-run at any time. Before each step, check the current state of `workspace:/.winter/config/winter-service-tmux/config.toml`. If the step is already done, **say so explicitly** ("`session_prefix` is already set to `wws` — skipping") and move on. Don't silent skip.

## How to run this guide

This is a guided walkthrough, not a script. Your job is to teach the user how their service orchestration is wired together while configuring it. Be verbose, be explicit, and be patient.

**Pacing rules — strict, no exceptions:**

- **One question at a time.** Never ask the user two things at once. No compound "and / or" questions. If a step needs three pieces of info, ask three times across three turns.
- **One step at a time.** Don't preemptively run a later step's commands while the current step is still in progress. Finish the work for the current step before starting the next.
- **Don't say step numbers.** Don't say "step 1 of 9" or "step 3" or "next step" — just describe what's happening and what's next. The user shouldn't be tracking a counter.
- **Speak before acting.** At the start of every step, send a short message that describes what's about to happen and *why* it matters. Don't dive straight into a question or a command.
- **Narrate actions.** Before running a command or editing a file, tell the user what's about to happen ("Writing `config.toml` with `session_prefix = "wws"` and a single shell service..."). After it runs, tell them what changed.
- **Don't pause between steps.** When a step's work is done, report what changed in one line and move directly into the next step. Don't ask "ready for the next one?" — just continue. The user can interrupt at any time.
- **Show, don't hide.** When you skip a step because the state is already correct, *show* what you found ("`config.toml` already declares 3 services: `backend`, `frontend`, `shell`. Skipping service declaration."). Never silent skip.

## Why this matters

When an agent spins up a feature environment, it runs `./up` to start all the services needed to use the application. `config.toml` tells the orchestrator exactly what to launch and in which tmux pane. Without it, `./up` errors out in every worktree.

This extension does **not** own `.winter.env`. The workspace base seeds it during `winter ws init <name>` with `WINTER_ENV`, `WINTER_ENV_INDEX`, and `WINTER_PORT_BASE`. The project's own `project-setup.md` appends project-specific variables (e.g. `BACKEND_PORT`, `DATABASE_URL`) below the managed block. This extension just *reads* `.winter.env` — when `env_file = ".winter.env"` is set in `config.toml`, the orchestrator sources it before launching each service pane, so every service starts with whatever vars the workspace and the project have populated.

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

**Explain first:** "Before changing anything, I need to know what's already there. `workspace:/.winter/config/winter-service-tmux/config.toml` is the canonical source of truth — if it exists, it tells me your current `session_prefix`, `env_file`, services, and any status URLs."

Check the current state:

```bash
if [[ -f ./.winter/config/winter-service-tmux/config.toml ]]; then
  cat ./.winter/config/winter-service-tmux/config.toml
else
  echo "(no config.toml)"
fi
```

**If a config already exists**, parse out and report what you found:

> "Your `config.toml` already exists: `session_prefix = "<value>"`, `env_file = "<value or 'unset'>"`, `<n>` services declared (`<name-list>`). Want to keep it as-is, replace it from scratch, or tweak something specific?"

- "keep": skip ahead to the "Validate" step and run the validator against the existing file — it's idempotent. Then offer the smoke test and continue into the final report.
- "replace": continue from the next step as if no file existed.
- "tweak": ask **"What would you like to change?"** and skip to the relevant step.

**If `config.toml` does not exist**, tell the user: "No `config.toml` yet — let's build one from scratch." Then continue.

### 2. Decide research vs. guided approach

**Explain first:** "Two ways to figure out what services need to run: I can research the project repos automatically (look at `package.json` scripts, `Procfile`, `docker-compose.yml`, server entry points, README/docs), or you can walk me through it. Researching is faster when there's an existing app to read; guided is better when you're starting fresh or have constraints the code doesn't capture."

Ask **one** question:

**"Want me to research automatically, or walk through it together?"**

- "automatic": tell the user "Spawning a research agent — it'll look at start scripts, server entry points, docker-compose, and READMEs across the project repos and report back what it finds." Then spawn an Opus subagent (always from the workspace root per the workspace rules) with a self-contained prompt:
  - Tell it which repos to inspect (read from `workspace:/.winter/config.toml` `[[project_repository]]` entries)
  - Ask it to find: start commands per service (`npm run dev`, `python manage.py runserver`, etc.), the directory each runs from, ports each service binds to, env vars each consumes from `.winter.env`, any docker-compose services that need to be `up`'d
  - Cap the response under 600 words
  - Tell it to be honest if a repo has no runtime services
  - Have it end with a synthesis: which services the manifest should declare, in what order, with what start commands
- "guided": fall through to the next step.

When the subagent reports back, present the findings to the user and ask **"Use these as the basis for `config.toml`?"** before proceeding.

### 3. Identify services

**Explain first:** "Each service becomes one tmux pane. For each one I need four things: the start command, the directory it runs from, any env vars it depends on from `.winter.env`, and whether it has a readiness probe. We'll go service by service — one at a time."

Ask **one** question:

**"What services need to run for the application to work? (e.g. backend API, frontend dev server, background worker, shell)"**

Once the user lists them, **for each service** ask in turn (one question per turn):

1. **"What's the start command for `<service>`?"** (e.g. `npm run dev`, `cargo run`, `python manage.py runserver`)
2. **"Which directory does it run from?"** (relative to the worktree root, e.g. `apps/backend`)
3. **"Which env vars from `.winter.env` does it need?"** (e.g. `$BACKEND_PORT`, `$DATABASE_URL`) — accept "none" as an answer.
4. **"Does `<service>` have a status health probe?"** Accept "none". This is reported by `status`; it does not make `./up` wait. For HTTP health checks, record `type = "url"` and the health URL (e.g. `http://localhost:${BACKEND_PORT}/health`). For shell checks, record `type = "cmd"` and the command that should exit 0 when ready (e.g. `pgrep -f my-worker`, run from the worktree root). Ask for a timeout only if the user wants a non-default value; otherwise omit it and use the default 5 seconds.

The start command and run directory combine into one `command` field in the manifest: a service that runs from the worktree root is just its command (`command = "npm run dev"`); a service in a subdirectory prepends a *relative* `cd` (`command = "cd apps/backend && npm run dev"`). Keep that `cd` relative — the orchestrator resets each pane to the worktree root before running, so the same command works on both `./up` and `./restart`.

Record each answer before moving to the next service. After all services are described, summarise back: "OK — you have `<n>` services: `<service-1>` running `<cmd-1>` from `<dir-1>`, `<service-2>` running `<cmd-2>` from `<dir-2>`, ..." and ask **"Anything to add, change, or remove?"** before continuing.

A common pattern is one pane per service plus an extra `shell` pane for ad-hoc commands. Suggest it if the user didn't include one: **"Add a `shell` pane for ad-hoc commands?"** (A shell pane uses an empty command: `command = ""`.)

### 4. Session prefix

**Explain first:** "Each worktree gets its own tmux session, named `<prefix>-<worktree>` — e.g. `<prefix>-alpha`, `<prefix>-beta`. The prefix should be short (2-4 chars), lowercase, alphanumeric, and distinct enough not to collide with other tmux sessions on the user's machine."

Suggest a prefix derived from the workspace directory name or the primary project name (initials of the workspace directory, an obvious acronym from the project, etc.) and **always prompt the user to confirm or override it** — never pick silently.

Ask **one** question:

**"I suggest `<derived>` as the tmux session prefix (sessions would be named `<derived>-alpha`, `<derived>-beta`, ...). Confirm, or enter a different prefix?"**

- "confirm" / "yes" / the same value: use it.
- different value: validate against the constraints (2-4 chars, lowercase, alphanumeric/`-`). If it fails any constraint, tell the user which one and ask again.

Record the confirmed value as `session_prefix`.

### 5. Environment file

**Explain first:** "If any service command or status health probe needs vars from `.winter.env` (the seeded `WINTER_PORT_BASE`, or anything `project-setup.md` appends), the orchestrator can source it before launching each pane and use it for health probe interpolation. If neither commands nor probes reference env vars, `env_file` can be left unset and panes start in a clean shell."

Look back at the env vars collected for each service command and health probe target. If **any** service answered with a non-"none" env var dependency, tell the user: "At least one service command or health probe uses `.winter.env` — I'll set `env_file = \".winter.env\"`." and continue.

If **no** service command or health probe uses env vars, ask **one** question:

**"No service command or health probe declared env-var dependencies. Set `env_file = \".winter.env\"` anyway (so worktree-managed vars like `$WINTER_PORT_BASE` are available in the shell pane), or leave it unset?"**

- "set" / "yes": record `env_file = ".winter.env"`.
- "unset" / "no": omit `env_file`.

### 6. Tmux session layout

**Explain first:** "tmux organises services along two axes: **windows** (separate full-screen tabs) and **splits** within a window (horizontal or vertical). The `layout-hook.sh` creates the pane geometry; the orchestrator then sends each service's command into its pane. Panes are addressed as `<window>.<pane>` (both zero-based) — these `target` values in the manifest must exactly match what the layout hook creates. See `winter-service-tmux:/workflow/layout-hook.sh.example` for a 2-window layout."

Based on the services collected, pick a default layout that keeps related services together (e.g. backend + frontend top, shell bottom). Tell the user: "Proposed layout for `<n>` panes: `<service-0>` → `0.0`, `<service-1>` → `0.1`, `<service-2>` → `1.0`." (Adjust to the actual count.)

Ask **one** question:

**"Use this layout, or describe a different one?"**

- "use this": continue.
- different layout: take the user's description literally — they know their screen.

Record each service's `<window>.<pane>` target. Every target must be unique.

### 7. Status URLs (optional)

**Explain first:** "`./status` reads each pane's last few lines and prints them. You can optionally inject a header above that with per-env URLs — useful for showing which ports are live. If you don't need one, skip this step."

Ask **one** question:

**"Want status URL entries? If yes, what should they show? (e.g. `http://localhost:$BACKEND_PORT` for the backend)"**

- "no" / "skip": omit `[[status.url]]` entries.
- otherwise: take the user's spec and translate it into `[[status.url]]` table entries. `${VAR}` placeholders in the `url` field are resolved from the env file at status time.

### 8. Write config.toml

**Explain first:** "Now I have everything needed to write `workspace:/.winter/config/winter-service-tmux/config.toml`. The annotated schema reference is `winter-service-tmux:/workflow/config.toml.example` — follow its structure and substitute the values we just collected."

Tell the user: "Writing `config.toml` with `session_prefix = "<prefix>"`, `env_file = "<value-or-omitted>"`, `<n>` services..."

Then write the file. Read `config.toml.example` and reproduce its structure, substituting:

- `session_prefix` ← the value confirmed in the session-prefix step.
- `env_file` ← `".winter.env"` if the env-file step recorded one; omit the key otherwise.
- `layout_hook` ← `"layout-hook.sh"` (the bare filename — resolved relative to the config dir where this file lives).
- `[[service]]` entries ← one table per service, with `name`, `target`, and `command`. Empty command (`command = ""`) for interactive panes.
- `[service.health]` subtables ← only for services that declared a status health probe. Place each subtable immediately after its matching `[[service]]`, before the next `[[service]]`. Use `type = "url"` or `type = "cmd"`, `target = "..."`, and optional `timeout = <seconds>`. `${VAR}` placeholders in `target` are resolved from `env_file`, the same way `[[status.url]]` entries are; bare `$VAR` is not interpolated.
- `[[status.url]]` entries ← one table per URL from the status-URLs step (omit section if none).

Confirm: "`config.toml` written at `workspace:/.winter/config/winter-service-tmux/config.toml`."

**Machine-specific overrides (mention, don't prompt):** the committed `config.toml` can be paired with a gitignored `config.local.toml` in the same directory for per-machine tweaks. The reader merges it on top using the same key-based semantics (scalars replace; services/URLs merge by `name`/`label`). Don't create one as part of this guide — just tell the user it exists: "If you ever need machine-specific overrides, drop a gitignored `config.local.toml` next to this file and it'll be merged on top." Only create it if the user explicitly asks; if you do, ensure it's gitignored.

### 9. Write layout-hook.sh

**Explain first:** "The orchestrator calls `layout-hook.sh` once per `./up`, after creating the tmux session and before sending any service commands. Its only job is to create the windows and panes the manifest's `[[service]]` targets refer to — nothing else. The annotated contract is `winter-service-tmux:/workflow/layout-hook.sh.example`."

Tell the user: "Writing `layout-hook.sh` to create the pane geometry we designed..."

Then write the file at `workspace:/.winter/config/winter-service-tmux/layout-hook.sh`. Read `layout-hook.sh.example` and follow its contract exactly:

- `set -euo pipefail`
- Assert `WINTER_TMUX_SESSION` and `WINTER_TMUX_WORKTREE_DIR` are set (the orchestrator always provides them).
- Create windows/panes to match the targets declared in `config.toml`. Pane `0.0` always exists after `tmux new-session` — don't create it. Use `tmux split-window` for splits within a window; use `tmux new-window` for additional windows.
- **DO NOT** `tmux send-keys`, source env files, or `cd`. The orchestrator does all of that.
- End with a `tmux select-pane` (and optionally `tmux select-window`) to set the focus on attach.

After writing, mark it executable:

```bash
chmod +x ./.winter/config/winter-service-tmux/layout-hook.sh
```

Confirm: "`layout-hook.sh` written and executable at `workspace:/.winter/config/winter-service-tmux/layout-hook.sh`."

### 10. Workspace singleton services (optional)

**Explain first:** "If your workspace needs shared infrastructure that should run once for the whole workspace — a database, a message broker, a container registry — you can mark a `[[service]]` entry with `scope = "workspace"` alongside the per-env ones (`scope` defaults to `"project"`). These run in a separate `<prefix>-workspace` tmux session at the workspace root, started via `winter service up workspace`. `winter service up <env>` ensures the workspace session is running first — so workspace singletons are guaranteed to be up when any env spins up via `winter service up`. Note: the env-root `./up` symlink does NOT auto-start the workspace session; if you use `alpha/up`, run `winter service up workspace` separately first."

Ask **one** question:

**"Does your workspace need any shared singleton services (e.g. a shared database or broker that all feature envs should share)? If yes, name them."**

- "no" / "none": skip this step and continue.
- otherwise: for each workspace service, ask in turn:
  1. **"What's the start command for `<service>`?"** — this is typically a blocking foreground command (e.g. `postgres -D /usr/local/var/postgres`, `rabbitmq-server`, `docker run -p 5432:5432 postgres:16`). The orchestrator kills the session to shut it down, so the command must run in the foreground.
  2. **"Which pane should it occupy?"** (e.g. `0.0`, `0.1`) — workspace pane targets are independent of env targets; the same address may appear in both without conflict.

After all workspace services are described, summarise back and ask **"Anything to add or change?"** before continuing.

**Write the workspace section.** Append to `workspace:/.winter/config/winter-service-tmux/config.toml`:

```toml
workspace_layout_hook = "workspace-layout-hook.sh"

[[service]]
name    = "<name>"
target  = "<window>.<pane>"
command = "<blocking-foreground-command>"
scope   = "workspace"
```

Then write `workspace:/.winter/config/winter-service-tmux/workspace-layout-hook.sh`, following the same contract as `layout-hook.sh` (layout only; see `winter-service-tmux:/workflow/layout-hook.sh.example`), but the orchestrator will supply `WINTER_ENV=workspace` and `WINTER_TMUX_WORKTREE_DIR=<workspace-root>` instead of per-env values (`WINTER_ENV_INDEX` and `WINTER_PORT_BASE` are **not** set for the workspace hook). Mark it executable:

```bash
chmod +x ./.winter/config/winter-service-tmux/workspace-layout-hook.sh
```

**Note on shutdown:** workspace services are shut down by `winter service down workspace`, which reaps the tmux session and kills each pane's process tree. This is best-effort — a service that forks to the background or detaches from the pane's process group may survive. Run commands in the foreground so the session reap reaches them.

Confirm: "Workspace singletons added to `config.toml`; `workspace-layout-hook.sh` written."

### 11. Validate

**Explain first:** "Before testing live, validate the manifest — the validator catches schema errors, duplicate targets, and missing-service issues before they reach a running tmux session."

Run the validator from the extension worktree (substituting the actual worktree path):

```bash
cd alpha/winter-service-tmux
PYTHONPATH=src python3 -m service_manifest.cli validate ../../
```

The validator resolves the config dir via `WINTER_EXT_CONFIG_DIR` when set, otherwise falls back to `<workspace-root>/.winter/config/winter-service-tmux/`.

If the validator reports errors, fix them in `config.toml` (or `layout-hook.sh` if the issue is an unreachable `layout_hook` path) before continuing.

Confirm: "Manifest validates cleanly."

### 12. Smoke test (optional)

**Explain first:** "Before declaring done, you can verify the full lifecycle in a real worktree."

Ask **one** question:

**"Run a smoke test in `alpha/` now (`cd alpha && ./up && ./status && ./down`), or skip?"**

- "skip" / "no": continue.
- "run" / "yes": tell the user "Running `./up` in `alpha/`..." then run it. After it returns, run `./status` and report the pane states. If any pane shows an error, tell the user exactly what failed and offer to revisit the relevant step. After confirming services look healthy, run `./down` to tear the session down cleanly.

### Final report

Summarise everything that happened in a single message:
- `config.toml` location: `workspace:/.winter/config/winter-service-tmux/config.toml` (created / replaced / unchanged)
- `session_prefix`
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
