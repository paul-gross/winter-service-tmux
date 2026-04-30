# Workflow setup walkthrough

This guide is an interactive walkthrough that produces `workspace:/ai/project/workflow.sh` — the shell script that configures the service management scripts (`./up`, `./down`, `./status`) for every feature worktree in the workspace. It defines what services run, how they start, and how they're organised in tmux panes.

Run it on a fresh workspace, or any time you want to (re)configure how services are launched — add new services, rename panes, change the session prefix, switch which env file gets sourced.

**Idempotent:** safe to re-run at any time. Before each step, check the current state of `workspace:/ai/project/workflow.sh`. If the step is already done, **say so explicitly** ("`SESSION_PREFIX` is already set to `wws` — skipping") and move on. Don't silent skip.

## How to run this guide

This is a guided walkthrough, not a script. Your job is to teach the user how their service orchestration is wired together while configuring it. Be verbose, be explicit, and be patient.

**Pacing rules — strict, no exceptions:**

- **One question at a time.** Never ask the user two things at once. No compound "and / or" questions. If a step needs three pieces of info, ask three times across three turns.
- **One step at a time.** Don't preemptively run a later step's commands while the current step is still in progress. Finish the work for the current step before starting the next.
- **Don't say step numbers.** Don't say "step 1 of 9" or "step 3" or "next step" — just describe what's happening and what's next. The user shouldn't be tracking a counter.
- **Speak before acting.** At the start of every step, send a short message that describes what's about to happen and *why* it matters. Don't dive straight into a question or a command.
- **Narrate actions.** Before running a command or editing a file, tell the user what's about to happen ("Writing `workflow.sh` with `SESSION_PREFIX=wws` and a single shell pane..."). After it runs, tell them what changed.
- **Don't pause between steps.** When a step's work is done, report what changed in one line and move directly into the next step. Don't ask "ready for the next one?" — just continue. The user can interrupt at any time.
- **Show, don't hide.** When you skip a step because the state is already correct, *show* what you found ("`workflow.sh` already declares 3 panes: `backend`, `frontend`, `shell`. Skipping pane layout."). Never silent skip.

## Why this matters

When an agent spins up a feature environment, it runs `./up` to start all the services needed to use the application. `workflow.sh` tells `./up` exactly what to launch and in which tmux pane. Without it, `./up` errors out in every worktree.

This extension does **not** own `.winter.env`. The workspace base seeds it during `winter ws init <name>` with `WINTER_ENV`, `WINTER_ENV_INDEX`, and `WINTER_PORT_BASE`. The project's own `project-setup.md` appends project-specific variables (e.g. `BACKEND_PORT`, `DATABASE_URL`) below the managed block. This extension just *reads* `.winter.env` — when `ENV_FILE=".winter.env"` is set in `workflow.sh`, the `up` script sources it before launching panes, so every service starts with whatever vars the workspace and the project have populated.

## Prerequisites

Before running this guide:
- The workspace has been set up via `/ws-setup` (or the underlying `winter ws init` has been run).
- The `winter-service-tmux` extension is installed (its standalone clone exists at `workspace:/winter-service-tmux/` and its `on_worktree_init` hook is wired into the workspace).
- Read `winter-service-tmux:/workflow/workflow.sh.example` to understand the structural template you'll be writing.

## Opening preamble (always send first)

Before doing anything, send a short orientation message, then continue straight into the first step:

> "I'll walk you through setting up `workflow.sh` so `./up`, `./down`, and `./status` know which services to run in each feature worktree. Stop me or ask questions at any time."

Don't wait for a "go" signal — just begin.

## Steps

### 1. Check existing workflow.sh

**Explain first:** "Before changing anything, I need to know what's already there. `workspace:/ai/project/workflow.sh` is the canonical source of truth — if it exists, it tells me your current `SESSION_PREFIX`, `ENV_FILE`, pane layout, and any custom `setup_panes`/`status_header` logic."

Check the current state:

```bash
test -f ./ai/project/workflow.sh && cat ./ai/project/workflow.sh || echo "(no workflow.sh)"
```

**If `workflow.sh` already exists**, parse out and report what you found:

> "Your `workflow.sh` already exists: `SESSION_PREFIX=<value>`, `ENV_FILE=<value or 'unset'>`, `PANE_NAMES=(<list>)`. Want to keep it as-is, replace it from scratch, or tweak something specific?"

- "keep": skip directly to the final report. Tell the user nothing changed.
- "replace": continue from the next step as if no file existed.
- "tweak": ask **"What would you like to change?"** and skip to the relevant step.

**If `workflow.sh` does not exist**, tell the user: "No `workflow.sh` yet — let's build one from scratch." Then continue.

### 2. Decide research vs. guided approach

**Explain first:** "Two ways to figure out what services need to run: I can research the project repos automatically (look at `package.json` scripts, `Procfile`, `docker-compose.yml`, server entry points, README/docs), or you can walk me through it. Researching is faster when there's an existing app to read; guided is better when you're starting fresh or have constraints the code doesn't capture."

Ask **one** question:

**"Want me to research automatically, or walk through it together?"**

- "automatic": tell the user "Spawning a research agent — it'll look at start scripts, server entry points, docker-compose, and READMEs across the project repos and report back what it finds." Then spawn an Opus subagent (always from the workspace root per the workspace rules) with a self-contained prompt:
  - Tell it which repos to inspect (read from `workspace:/.winter/config.toml` `[[project_repository]]` entries)
  - Ask it to find: start commands per service (`npm run dev`, `python manage.py runserver`, etc.), the directory each runs from, ports each service binds to, env vars each consumes from `.winter.env`, any docker-compose services that need to be `up`'d
  - Cap the response under 600 words
  - Tell it to be honest if a repo has no runtime services
  - Have it end with a synthesis: which panes the workflow.sh should declare, in what order, with what start commands
- "guided": fall through to the next step.

When the subagent reports back, present the findings to the user and ask **"Use these as the basis for `workflow.sh`?"** before proceeding.

### 3. Identify services

**Explain first:** "Each service becomes one tmux pane. For each one I need three things: the start command, the directory it runs from, and any env vars it depends on from `.winter.env`. We'll go service by service — one at a time."

Ask **one** question:

**"What services need to run for the application to work? (e.g. backend API, frontend dev server, background worker, shell)"**

Once the user lists them, **for each service** ask in turn (one question per turn):

1. **"What's the start command for `<service>`?"** (e.g. `npm run dev`, `cargo run`, `python manage.py runserver`)
2. **"Which directory does it run from?"** (relative to the worktree root, e.g. `apps/backend`)
3. **"Which env vars from `.winter.env` does it need?"** (e.g. `$BACKEND_PORT`, `$DATABASE_URL`) — accept "none" as an answer.

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

### 6. Pane layout

**Explain first:** "tmux supports horizontal and vertical splits. The `setup_panes` function uses `tmux split-window` to lay them out and `tmux send-keys` to launch each service. Common patterns: vertical split for two services (top/bottom), 2x2 grid for four, or horizontal-then-vertical for three (one big pane + two smaller stacked)."

Based on the services collected, pick a default layout that keeps related services together (e.g. backend + frontend top, shell bottom). Tell the user: "Proposed layout for `<n>` panes: `<pane-0>` top-left, `<pane-1>` top-right, `<pane-2>` bottom-full." (Adjust to the actual count.)

Ask **one** question:

**"Use this layout, or describe a different one?"**

- "use this": continue.
- different layout: take the user's description literally — they know their screen.

Record the pane order (this becomes the `PANE_NAMES` array) and the split commands needed for `setup_panes`.

### 7. Status header (optional)

**Explain first:** "`./status` reads each pane's last few lines and prints them. You can optionally inject a header above that — useful for showing per-environment URLs, ports, or any quick orientation info. If you don't need one, leave it as a no-op."

Ask **one** question:

**"Want a `./status` header? If yes, what should it show? (e.g. `http://localhost:$BACKEND_PORT` for the backend, the worktree's database name)"**

- "no" / "skip": leave `status_header()` as `:` (no-op).
- otherwise: take the user's spec and translate it into shell that reads `$ENV_PATH` (when set) and echoes the requested info.

### 8. Write workflow.sh

**Explain first:** "Now I have everything needed to write `workspace:/ai/project/workflow.sh`. Use `winter-service-tmux:/workflow/workflow.sh.example` as the structural template — same shape, customised values."

Tell the user: "Writing `workflow.sh` with `SESSION_PREFIX=<prefix>`, `ENV_FILE=<value-or-unset>`, `PANE_NAMES=(<list>)`, and `setup_panes` for `<n>` panes..."

Then write the file. The structure must be:

```bash
#!/usr/bin/env bash
# Project workflow configuration for winter workspace scripts.

SESSION_PREFIX="<prefix>"

# Optional: set to source .winter.env before launching panes
ENV_FILE=".winter.env"

PANE_NAMES=("<pane-0>" "<pane-1>" ...)

setup_panes() {
  local session="$1" dir="$2" name="$3"
  local env_cmd=""
  if [[ -n "${ENV_PATH:-}" ]]; then
    env_cmd="source '$ENV_PATH' && "
  fi

  # Pane 0: <pane-0>
  tmux send-keys -t "$session:0.0" \
    "${env_cmd}cd $dir/<dir-0> && <cmd-0>" Enter

  # Splits and additional panes here, one block per pane
  # ...

  tmux select-pane -t "$session:0.0"
}

status_header() {
  local name="$1" dir="$2"
  # Custom header logic, or `:` for no-op
  :
}
```

After writing, mark it executable:

```bash
chmod +x ./ai/project/workflow.sh
```

Confirm: "`workflow.sh` written and executable at `workspace:/ai/project/workflow.sh`."

### 9. Smoke test (optional)

**Explain first:** "Before declaring done, you can verify the script parses and `./up` reaches the launch step in a real worktree. The cheapest test: run `./up` in `alpha/` and check `./status` afterward."

Ask **one** question:

**"Run a smoke test in `alpha/` now (`cd alpha && ./up && ./status`), or skip?"**

- "skip" / "no": continue.
- "run" / "yes": tell the user "Running `./up` in `alpha/`..." then run it. After it returns, run `./status` and report the pane states. If any pane shows an error, tell the user exactly what failed and offer to revisit the relevant step.

### Final report

Summarise everything that happened in a single message:
- `workflow.sh` location: `workspace:/ai/project/workflow.sh` (created / replaced / unchanged)
- `SESSION_PREFIX`
- `ENV_FILE` (value or "unset")
- `PANE_NAMES` (the list)
- Number of panes and their start commands
- Smoke test: ran / skipped / failed (if failed, what to fix)
- Any manual steps still pending

End with:

> "Workflow setup complete. You can re-run this guide any time — it's idempotent and will only apply changes that are still needed."
