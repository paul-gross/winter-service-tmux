# Workflow setup walkthrough

This guide is an interactive walkthrough that produces `workspace:/ai/project/setup-tmux.sh` — the shell script that configures the service management scripts (`./up`, `./down`, `./status`) for every feature worktree in the workspace. It defines what services run, how they start, and how they're organised in tmux panes.

Run it on a fresh workspace, or any time you want to (re)configure how services are launched — add new services, rename panes, change the session prefix, switch which env file gets sourced.

**Idempotent:** safe to re-run at any time. Before each step, check the current state of `workspace:/ai/project/setup-tmux.sh`. If the step is already done, **say so explicitly** ("`SESSION_PREFIX` is already set to `wws` — skipping") and move on. Don't silent skip.

## How to run this guide

This is a guided walkthrough, not a script. Your job is to teach the user how their service orchestration is wired together while configuring it. Be verbose, be explicit, and be patient.

**Pacing rules — strict, no exceptions:**

- **One question at a time.** Never ask the user two things at once. No compound "and / or" questions. If a step needs three pieces of info, ask three times across three turns.
- **One step at a time.** Don't preemptively run a later step's commands while the current step is still in progress. Finish the work for the current step before starting the next.
- **Don't say step numbers.** Don't say "step 1 of 9" or "step 3" or "next step" — just describe what's happening and what's next. The user shouldn't be tracking a counter.
- **Speak before acting.** At the start of every step, send a short message that describes what's about to happen and *why* it matters. Don't dive straight into a question or a command.
- **Narrate actions.** Before running a command or editing a file, tell the user what's about to happen ("Writing `setup-tmux.sh` with `SESSION_PREFIX=wws` and a single shell pane..."). After it runs, tell them what changed.
- **Don't pause between steps.** When a step's work is done, report what changed in one line and move directly into the next step. Don't ask "ready for the next one?" — just continue. The user can interrupt at any time.
- **Show, don't hide.** When you skip a step because the state is already correct, *show* what you found ("`setup-tmux.sh` already declares 3 panes: `backend`, `frontend`, `shell`. Skipping tmux session layout."). Never silent skip.

## Why this matters

When an agent spins up a feature environment, it runs `./up` to start all the services needed to use the application. `setup-tmux.sh` tells `./up` exactly what to launch and in which tmux pane. Without it, `./up` errors out in every worktree.

This extension does **not** own `.winter.env`. The workspace base seeds it during `winter ws init <name>` with `WINTER_ENV`, `WINTER_ENV_INDEX`, and `WINTER_PORT_BASE`. The project's own `project-setup.md` appends project-specific variables (e.g. `BACKEND_PORT`, `DATABASE_URL`) below the managed block. This extension just *reads* `.winter.env` — when `ENV_FILE=".winter.env"` is set in `setup-tmux.sh`, the `up` script sources it before launching panes, so every service starts with whatever vars the workspace and the project have populated.

## Prerequisites

Before running this guide:
- The workspace has been set up via `/ws-setup` (or the underlying `winter ws init` has been run).
- The `winter-service-tmux` extension is installed (its standalone clone exists at `workspace:/winter-service-tmux/` and its `on_env_init` hook is wired into the workspace).
- Read `winter-service-tmux:/workflow/setup-tmux.sh.example` to understand the structural template you'll be writing.

## Opening preamble (always send first)

Before doing anything, send a short orientation message, then continue straight into the first step:

> "I'll walk you through setting up `setup-tmux.sh` so `./up`, `./down`, and `./status` know which services to run in each feature worktree. Stop me or ask questions at any time."

Don't wait for a "go" signal — just begin.

## Steps

### 1. Check existing setup-tmux.sh

**Explain first:** "Before changing anything, I need to know what's already there. `workspace:/ai/project/setup-tmux.sh` is the canonical source of truth — if it exists, it tells me your current `SESSION_PREFIX`, `ENV_FILE`, tmux session layout, and any custom `setup_tmux`/`status_header` logic."

**If the existing file defines `setup_panes` (the pre-multi-window name)**, flag it: "Your `setup-tmux.sh` defines `setup_panes` — that's the old name. The current scripts expect `setup_tmux`. I'll rename it (and update any references) as part of this guide." Treat that as a forced "replace" path: rebuild the file from the current values rather than asking the user to "keep" the broken version.

**Legacy filename:** the config used to be called `workflow.sh`. The scripts still source `workflow.sh` as a fallback, but the canonical name is now `setup-tmux.sh`. If you find a `workflow.sh` and no `setup-tmux.sh`, flag it: "Your config is at the legacy path `workflow.sh` — I'll migrate it to `setup-tmux.sh` as part of this guide (`git mv ai/project/workflow.sh ai/project/setup-tmux.sh`), then apply any edits." Migrate before writing.

Check the current state (prefer the canonical name, fall back to the legacy one):

```bash
if [[ -f ./ai/project/setup-tmux.sh ]]; then
  cat ./ai/project/setup-tmux.sh
elif [[ -f ./ai/project/workflow.sh ]]; then
  echo "(legacy workflow.sh — migrate to setup-tmux.sh)"; cat ./ai/project/workflow.sh
else
  echo "(no setup-tmux.sh)"
fi
```

**If a config already exists** (at either name), parse out and report what you found:

> "Your `setup-tmux.sh` already exists: `SESSION_PREFIX=<value>`, `ENV_FILE=<value or 'unset'>`, `WINTER_TMUX_SERVICE_NAMES=(<list>)`, `winter_service_cmd` declarations for `<n>` services. Want to keep it as-is, replace it from scratch, or tweak something specific?"

**Pre-`./restart` config (legacy):** if `setup_tmux` hand-writes its own `tmux send-keys` start strings with **no** `winter_service_cmd` declarations, `./up`/`./down`/`./status` still work but `./restart` errors. Lift the start commands into `winter_service_cmd <name> "<command>"` declarations and switch `setup_tmux` to `winter_tmux_send_service`. Flag it: "Your `setup-tmux.sh` predates `./restart` — I'll lift its start commands into `winter_service_cmd` declarations so `./restart` works." Treat that as a forced "replace" path.

- "keep": skip ahead to the "Generate setup-tmux.md" step and run the generator against the existing `setup-tmux.sh` — it's idempotent and writes byte-identical content when nothing changed. Then offer the smoke test and continue into the final report.
- "replace": continue from the next step as if no file existed.
- "tweak": ask **"What would you like to change?"** and skip to the relevant step.

**If `setup-tmux.sh` does not exist**, tell the user: "No `setup-tmux.sh` yet — let's build one from scratch." Then continue.

### 2. Decide research vs. guided approach

**Explain first:** "Two ways to figure out what services need to run: I can research the project repos automatically (look at `package.json` scripts, `Procfile`, `docker-compose.yml`, server entry points, README/docs), or you can walk me through it. Researching is faster when there's an existing app to read; guided is better when you're starting fresh or have constraints the code doesn't capture."

Ask **one** question:

**"Want me to research automatically, or walk through it together?"**

- "automatic": tell the user "Spawning a research agent — it'll look at start scripts, server entry points, docker-compose, and READMEs across the project repos and report back what it finds." Then spawn an Opus subagent (always from the workspace root per the workspace rules) with a self-contained prompt:
  - Tell it which repos to inspect (read from `workspace:/.winter/config.toml` `[[project_repository]]` entries)
  - Ask it to find: start commands per service (`npm run dev`, `python manage.py runserver`, etc.), the directory each runs from, ports each service binds to, env vars each consumes from `.winter.env`, any docker-compose services that need to be `up`'d
  - Cap the response under 600 words
  - Tell it to be honest if a repo has no runtime services
  - Have it end with a synthesis: which panes the setup-tmux.sh should declare, in what order, with what start commands
- "guided": fall through to the next step.

When the subagent reports back, present the findings to the user and ask **"Use these as the basis for `setup-tmux.sh`?"** before proceeding.

### 3. Identify services

**Explain first:** "Each service becomes one tmux pane. For each one I need three things: the start command, the directory it runs from, and any env vars it depends on from `.winter.env`. We'll go service by service — one at a time."

Ask **one** question:

**"What services need to run for the application to work? (e.g. backend API, frontend dev server, background worker, shell)"**

Once the user lists them, **for each service** ask in turn (one question per turn):

1. **"What's the start command for `<service>`?"** (e.g. `npm run dev`, `cargo run`, `python manage.py runserver`)
2. **"Which directory does it run from?"** (relative to the worktree root, e.g. `apps/backend`)
3. **"Which env vars from `.winter.env` does it need?"** (e.g. `$BACKEND_PORT`, `$DATABASE_URL`) — accept "none" as an answer.

The start command and run directory combine into one **`winter_service_cmd` declaration** later (written in step 8): a service that runs from the worktree root is just its command (`winter_service_cmd backend "npm run dev"`); a service in a subdirectory prepends a *relative* `cd` (`winter_service_cmd backend "cd apps/backend && npm run dev"`). Keep that `cd` relative — the launch helper resets each pane to the worktree root before running, so the same command works on both `./up` and `./restart`.

Record each answer before moving to the next service. After all services are described, summarise back: "OK — you have `<n>` services: `<service-1>` running `<cmd-1>` from `<dir-1>`, `<service-2>` running `<cmd-2>` from `<dir-2>`, ..." and ask **"Anything to add, change, or remove?"** before continuing.

A common pattern is one pane per service plus an extra `shell` pane for ad-hoc commands. Suggest it if the user didn't include one: **"Add a `shell` pane for ad-hoc commands?"**

### 4. Session prefix

**Explain first:** "Each worktree gets its own tmux session, named `<prefix>-<worktree>` — e.g. `<prefix>-alpha`, `<prefix>-beta`. The prefix should be short (2-4 chars), lowercase, alphanumeric, and distinct enough not to collide with other tmux sessions on the user's machine."

Suggest a prefix derived from the workspace directory name or the primary project name (initials of the workspace directory, an obvious acronym from the project, etc.) and **always prompt the user to confirm or override it** — never pick silently.

Ask **one** question:

**"I suggest `<derived>` as the tmux session prefix (sessions would be named `<derived>-alpha`, `<derived>-beta`, ...). Confirm, or enter a different prefix?"**

- "confirm" / "yes" / the same value: use it.
- different value: validate against the constraints (2-4 chars, lowercase, alphanumeric/`-`). If it fails any constraint, tell the user which one and ask again.

Record the confirmed value as `SESSION_PREFIX`.

### 5. Environment file

**Explain first:** "If any service needs vars from `.winter.env` (the seeded `WINTER_PORT_BASE`, or anything `project-setup.md` appends), the `up` script can source it before launching panes. If no service references env vars, `ENV_FILE` can be left unset and panes start in a clean shell."

Look back at the env vars collected for each service. If **any** service answered with a non-"none" env var dependency, tell the user: "At least one service uses `.winter.env` — I'll set `ENV_FILE=\".winter.env\"`." and continue.

If **no** service uses env vars, ask **one** question:

**"No service declared env-var dependencies. Set `ENV_FILE=\".winter.env\"` anyway (so worktree-managed vars like `$WINTER_PORT_BASE` are available in the shell pane), or leave it unset?"**

- "set" / "yes": record `ENV_FILE=".winter.env"`.
- "unset" / "no": leave `ENV_FILE` unset.

### 6. Tmux session layout

**Explain first:** "tmux organises services along two axes: **windows** (separate full-screen tabs, created with `tmux new-window`) and **splits** within a window (horizontal or vertical, created with `tmux split-window`). The `setup_tmux` function uses both to lay out the session; each service is then launched into its pane with `winter_tmux_send_service` (not hand-written `tmux send-keys` — see step 8). Common single-window patterns: vertical split for two services (top/bottom), 2x2 grid for four, or horizontal-then-vertical for three (one big pane + two smaller stacked). Reach for multiple windows when one gets crowded, or to group services logically (e.g. application services in window 0, ad-hoc shells in window 1). Panes are addressed as `<window>.<pane>` — see `setup-tmux.sh.example` for a 2-window layout."

Based on the services collected, pick a default layout that keeps related services together (e.g. backend + frontend top, shell bottom). Tell the user: "Proposed layout for `<n>` panes: `<pane-0>` top-left, `<pane-1>` top-right, `<pane-2>` bottom-full." (Adjust to the actual count.)

Ask **one** question:

**"Use this layout, or describe a different one?"**

- "use this": continue.
- different layout: take the user's description literally — they know their screen.

Record the pane order (this becomes the `WINTER_TMUX_SERVICE_NAMES` array) and the split commands needed for `setup_tmux`. For single-window layouts, `WINTER_TMUX_SERVICE_NAMES` entries are bare names (`"backend"`, `"frontend"`) — each one's pane index is its array position within window 0. For multi-window layouts, use the prefixed form `"<window>.<pane>:<name>"` for **every** entry (even the window-0 ones), e.g. `("0.0:backend" "0.1:frontend" "1.0:worker")`. The bare form is a single-window shorthand; mixing it with prefixed entries technically works but reads ambiguously — pick one form per layout.

### 7. Status header (optional)

**Explain first:** "`./status` reads each pane's last few lines and prints them. You can optionally inject a header above that — useful for showing per-environment URLs, ports, or any quick orientation info. If you don't need one, leave it as a no-op."

Ask **one** question:

**"Want a `./status` header? If yes, what should it show? (e.g. `http://localhost:$BACKEND_PORT` for the backend, the worktree's database name)"**

- "no" / "skip": leave `status_header()` as `:` (no-op).
- otherwise: take the user's spec and translate it into shell that reads `$ENV_PATH` (when set) and echoes the requested info.

### 8. Write setup-tmux.sh

**Explain first:** "Now I have everything needed to write `workspace:/ai/project/setup-tmux.sh`. The canonical structural template is `winter-service-tmux:/workflow/setup-tmux.sh.example` — follow it exactly and substitute the values we just collected."

Tell the user: "Writing `setup-tmux.sh` with `SESSION_PREFIX=<prefix>`, `ENV_FILE=<value-or-unset>`, `WINTER_TMUX_SERVICE_NAMES=(<list>)`, `winter_service_cmd` declarations for `<n>` services, and `setup_tmux`..."

Then write the file. Read `setup-tmux.sh.example` and reproduce its structure exactly, substituting:

- `SESSION_PREFIX` ← the value confirmed in the session-prefix step.
- `ENV_FILE` ← `".winter.env"` if the env-file step recorded one; omit the line otherwise.
- `WINTER_TMUX_SERVICE_NAMES` ← the array built in the tmux-session-layout step. For multi-window layouts, use the `"<window>.<pane>:<name>"` form for **every** entry (don't mix bare and prefixed); for single-window layouts, the bare form is fine throughout.
- `winter_service_cmd` declarations ← one `winter_service_cmd <name> "<command>"` line per service, built from the start commands and run directories collected in the identify-services step. Root-level service → just its command (`winter_service_cmd backend "npm run start:dev"`); subdirectory service → relative `cd` prefix (`winter_service_cmd backend "cd apps/backend && npm run start:dev"`). Give a purely interactive pane (e.g. `shell`) an empty command (`winter_service_cmd shell ""`). These declarations are the single source of truth `setup_tmux` and `./restart` both read, so every name in `WINTER_TMUX_SERVICE_NAMES` needs one. (`winter_service_cmd` is provided by `winter-service-tmux.sh`.)
- `setup_tmux` body ← the layout: `tmux split-window` / `tmux new-window` calls for the windows and panes you designed, launching each service with `winter_tmux_send_service "$session" "<window>.<pane>" "<name>"` (the helper from `winter-service-tmux.sh` that looks up the `winter_service_cmd` command, resets the pane cwd, sources the env file, and sends it). Do **not** hand-write `tmux send-keys` start strings — route every launch through the helper so `./up` and `./restart` stay identical. The example shows both vertical splits within a window and `tmux new-window` to start a second window — use whichever the layout requires.
- `status_header` ← the body from the status-header step, or `:` for the no-op default.

After writing, mark it executable:

```bash
chmod +x ./ai/project/setup-tmux.sh
```

Confirm: "`setup-tmux.sh` written and executable at `workspace:/ai/project/setup-tmux.sh`."

**Machine-specific overrides (mention, don't prompt):** the committed `setup-tmux.sh` can be paired with a gitignored `setup-tmux.local.sh` for per-machine tweaks. `./up`, `./down`, `./status`, and `./restart` source the local file **on top of** the committed one (overlay), so it can override `SESSION_PREFIX`/`ENV_FILE`, redefine `setup_tmux`/`status_header`, extend `WINTER_TMUX_SERVICE_NAMES`, or add/override a service's command with another `winter_service_cmd` call, without touching version control. Don't create one as part of this guide — just tell the user it exists: "If you ever need machine-specific overrides, drop a gitignored `setup-tmux.local.sh` next to this file and it'll be layered on top." Only create it if the user explicitly asks; if you do, ensure it's gitignored.

### 9. Generate setup-tmux.md (agent context)

**Explain first:** "Agents read service output via `tmux capture-pane -t <session>:<window>.<pane>`. Without help, they'd have to open `setup-tmux.sh` and count `WINTER_TMUX_SERVICE_NAMES` indices to translate a service name into a pane target. `setup-tmux.md` is a short, agent-readable sibling that lists each declared service alongside its `<window>.<pane>` and the capture-pane template. It is **generated from `setup-tmux.sh`, never hand-written** — the `render-setup-md.sh` generator reads `SESSION_PREFIX` and `WINTER_TMUX_SERVICE_NAMES` from the config you just wrote and emits the canonical content. Going forward, `setup-tmux.md` is regenerated automatically on every workspace reconcile (`winter ws init`) via the `on_workspace_reconcile` hook. Run it manually now (and whenever `setup-tmux.sh` changes outside of a reconcile); `winter doctor` flags drift if it falls out of sync."

Run the generator from the workspace root (where the rest of this guide runs), writing to the canonical path (resolve `winter-service-tmux:` via the `# Winter Extensions` block in workspace `CLAUDE.md`):

```bash
"<ext-dir>/workflow/render-setup-md.sh" "$PWD" > ./ai/project/setup-tmux.md
```

The generator resolves each service to its target (bare entry → window 0 at its array index; `"<window>.<pane>:<name>"` → the explicit target), keeps the `<SESSION_PREFIX>`/`<worktree>`/`<session>`/`<window>`/`<pane>`/`<service>` placeholders literal for the reading agent to fill per-worktree, and is byte-stable across runs.

Tell the user "Generated `setup-tmux.md` with `<n>` services: `backend` → `0.0`, `frontend` → `0.1`, ..." (read the targets back from the output).

Confirm: "`setup-tmux.md` is at `workspace:/ai/project/setup-tmux.md`, generated from `setup-tmux.sh`. Future reconciles keep it in sync automatically; `winter doctor` warns if it ever drifts."

### 10. Smoke test (optional)

**Explain first:** "Before declaring done, you can verify the script parses and `./up` reaches the launch step in a real worktree. The cheapest test: run `./up` in `alpha/` and check `./status` afterward."

Ask **one** question:

**"Run a smoke test in `alpha/` now (`cd alpha && ./up && ./status`), or skip?"**

- "skip" / "no": continue.
- "run" / "yes": tell the user "Running `./up` in `alpha/`..." then run it. After it returns, run `./status` and report the pane states. If any pane shows an error, tell the user exactly what failed and offer to revisit the relevant step.

### Final report

Summarise everything that happened in a single message:
- `setup-tmux.sh` location: `workspace:/ai/project/setup-tmux.sh` (created / replaced / unchanged)
- `SESSION_PREFIX`
- `ENV_FILE` (value or "unset")
- `WINTER_TMUX_SERVICE_NAMES` (the list)
- Number of panes and their start commands
- `setup-tmux.md`: `workspace:/ai/project/setup-tmux.md` (written / unchanged)
- Smoke test: ran / skipped / failed (if failed, what to fix)
- Any manual steps still pending

End with:

> "Workflow setup complete. You can re-run this guide any time — it's idempotent and will only apply changes that are still needed."
