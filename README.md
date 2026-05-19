# TaskForge

Turn task-manager work items into agent implementation jobs.

This is meant for a personal or small-team workflow where a product manager writes
tasks and a tech lead controls when implementation agents start work.

The current implementation ships with Trello as the task-manager adapter and
Codex as the implementation-agent adapter. The project name is intentionally
neutral so other task managers and agents can be added later.

## What It Does

When a Trello card enters your configured **To Do** list, the service can:

1. Save the Trello event to a durable SQLite job queue.
2. Validate that the card has required product sections.
3. Optionally require a specific Trello label before starting.
4. Create or resume a git branch and worktree for that card.
5. Run Codex with the card as the task prompt.
6. Commit local changes.
7. Optionally push the branch and open a GitHub pull request.
8. Update the Trello card with status, comments, labels, branch, changed files, and PR URL.

It also has a local dashboard, dry-run mode, cleanup commands, and Trello comment
commands like `/codex retry`.

## Requirements

- Python 3.11 or newer
- Git
- A Trello board
- A repo that Codex is allowed to modify
- A public URL for Trello webhooks, for example from a tunnel or deployed server
- Codex CLI installed if you want real implementation runs
- Optional: Docker
- Optional: GitHub token for automatic PR creation

The runtime uses only the Python standard library.

## Quick Start: Safe Dry Run

Start with dry run. This validates Trello cards and exercises the queue without
running Codex, committing, pushing, or opening PRs.

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```bash
DRY_RUN=true
TRELLO_KEY=...
TRELLO_TOKEN=...
TRELLO_CALLBACK_URL=https://your-public-url.example.com/webhooks/trello
TRELLO_BOARD_ID=...
TRELLO_TODO_LIST_ID=...
TARGET_REPO=/absolute/path/to/your/repo
REPO_ALLOWLIST=/absolute/path/to/your/repo
WORKTREE_ROOT=/absolute/path/to/codex-worktrees
```

Validate configuration:

```bash
python3 -m taskforge validate-config
```

Run the service:

```bash
python3 -m taskforge serve
```

In another terminal, register the Trello webhook:

```bash
python3 -m taskforge register-webhook
```

Open the dashboard:

```text
http://localhost:8080/dashboard
```

Now create a Trello card in the configured To Do list using the card template
below. In dry run, the service should comment that the card passed validation and
was not executed.

## Trello Setup

You need these IDs:

- Board ID: `TRELLO_BOARD_ID`
- To Do list ID: `TRELLO_TODO_LIST_ID`
- Optional Question list ID: `TRELLO_QUESTION_LIST_ID`
- Optional Review list ID: `TRELLO_REVIEW_LIST_ID`
- Optional Done list ID: `TRELLO_DONE_LIST_ID`
- Optional status label IDs
- Optional start label IDs

One practical way to find IDs is to open Trello in your browser and use Trello's
API responses or board export. The service expects Trello IDs, not display names.

### Status Labels

These are optional. If set, the service keeps one current automation status label
on each card:

```bash
TRELLO_RUNNING_LABEL_ID=
TRELLO_QUESTION_LABEL_ID=
TRELLO_REVIEW_LABEL_ID=
TRELLO_DONE_LABEL_ID=
TRELLO_FAILED_LABEL_ID=
```

### Start Label Gate

If you only want Codex to start when a card has an approved label, set:

```bash
TRELLO_START_LABEL_IDS=label-id-1,label-id-2
```

If this is empty, any valid card entering the To Do list can start. If it is set,
the card must have at least one matching label ID. The service checks this at
webhook time and again after refreshing the card before running Codex.

## Product Card Template

By default, cards must include:

```md
## Problem

What user or business problem are we solving?

## Scope

What should be changed?

## Acceptance Criteria

- What must be true when the work is complete?

## Test Plan

- How should Codex or the reviewer verify this?
```

Print your active template:

```bash
python3 -m taskforge card-template
```

Change required sections with:

```bash
REQUIRED_CARD_SECTIONS=Problem,Scope,Acceptance Criteria,Test Plan
```

If a card is missing required sections, Codex does not start. The card is moved
to the Question list if configured, and the missing sections are posted as a
Trello comment.

## Enable Real Codex Runs

After dry run works, disable it:

```bash
DRY_RUN=false
```

Set the command used to run Codex:

```bash
CODEX_COMMAND_TEMPLATE='codex exec --cd {workdir} --dangerously-bypass-approvals-and-sandbox -'
```

When running with Docker Compose, the image installs the Codex CLI and mounts
`${CODEX_HOME:-/root/.codex}` into the service container so the CLI can use the
same authentication as the host.
It also mounts `${SSH_HOME:-/root/.ssh}` read-only so SSH remotes such as
`git@github.com:owner/repo.git` can be pushed from inside the container.
The trailing `-` tells `codex exec` to read TaskForge's generated prompt from
stdin.
The bypass flag is used because TaskForge runs Codex inside an already isolated
Docker service; Codex's workspace sandbox can fail in Docker when user
namespaces are unavailable.

Supported placeholders:

- `{prompt_file}`
- `{workdir}`
- `{branch}`
- `{card_id}`
- `{card_url}`
- `{card_name}`

Each task gets its own branch and worktree under `WORKTREE_ROOT`.
Before creating a new task worktree, TaskForge fetches `REMOTE_NAME` and
fast-forwards `BASE_BRANCH` so the task branch starts from the latest remote
base branch. If the local base branch has diverged from the remote branch, the
task stops so the repository can be reconciled first.

Codex should write this result file before exiting:

```json
{"status":"review","summary":"What changed and how it was checked.","question":""}
```

Allowed statuses:

- `question`: blocked, needs PM or tech lead input
- `review`: implementation is ready for tech lead review
- `done`: complete without review, only use when your workflow allows it

If no result file exists and the command exits successfully, the service treats
the task as `review`.

## Optional GitHub PRs

To push branches and create pull requests:

```bash
ENABLE_GIT_PUSH=true
ENABLE_PR_CREATION=true
GITHUB_TOKEN=...
GITHUB_REPO=owner/repo
PR_BASE_BRANCH=main
REMOTE_NAME=origin
```

The local git remote must already be authenticated for `git push`.

When enabled, the service commits Codex changes, pushes the branch, opens a PR,
comments the PR URL on Trello, and reads GitHub combined commit status when
available.

## Running

Run directly:

```bash
python3 -m taskforge serve
```

Run with Docker:

```bash
docker compose up --build
```

Docker Compose stores Taskforge state in `./data` by default so card
branch/worktree records survive container rebuilds.

There is also a systemd template:

```text
deploy/taskforge.service
```

Useful endpoints:

- `GET /healthz`
- `GET /dashboard`
- `GET /api/state`
- `HEAD /webhooks/trello`
- `POST /webhooks/trello`

## Operator Commands

Inspect state:

```bash
python3 -m taskforge status
```

Manually run or resume a card:

```bash
python3 -m taskforge run-card <trello-card-id>
```

Cleanup completed-card worktrees:

```bash
python3 -m taskforge cleanup --dry-run
python3 -m taskforge cleanup
```

Trello comment commands:

- `/codex retry`
- `/codex stop`
- `/codex done`
- `/codex cleanup`
- `/codex help`
- `/codex <review feedback>` on a card in the Review list resumes the recorded
  branch/worktree, asks Codex to address the tech lead comment, pushes the branch,
  and keeps the existing PR URL on the card.

## State, Jobs, And Logs

`STATE_FILE` stores SQLite state and queued jobs:

```bash
STATE_FILE=.taskforge-state.sqlite3
```

When running with Docker Compose, `STATE_FILE` is overridden to
`/data/taskforge-state.sqlite3` unless `TASKFORGE_STATE_FILE` is set. The
compose file mounts `./data` at `/data`, so review feedback commands can reuse
the recorded branch/worktree after the container is recreated.

If the service restarts, running jobs are requeued. If a card was already marked
running for the same Trello action, the worker can resume the existing branch and
worktree.

Per-run logs are written inside each worktree:

```text
.codex/logs/
```

When running with Docker Compose, stream service logs with:

```bash
docker compose logs -f --tail=200
```

The service logs webhook receipt details, queue decisions, worker job lifecycle,
card validation decisions, runner results, status moves, push/PR steps, and
errors. Card descriptions and command text are summarized by length instead of
being printed in full.

The dashboard also exposes a Logs button for each job. For running jobs, it
polls the current Codex run log so output appears as the subprocess writes it.

## Safety Settings

Use these for a personal server:

```bash
REPO_ALLOWLIST=/absolute/path/to/your/repo
MAX_CONCURRENT_JOBS=1
CODEX_TIMEOUT_SECONDS=7200
DRY_RUN=true
```

Important: this service runs a command that can edit your repository. Keep
`REPO_ALLOWLIST` narrow, use dry run first, and avoid pointing it at repos you do
not want modified.

## Troubleshooting

`validate-config` fails:

- Check required Trello fields in `.env`.
- If PR creation is enabled, check `GITHUB_TOKEN` and `GITHUB_REPO`.
- If `REPO_ALLOWLIST` is set, `TARGET_REPO` must match it exactly after path resolution.

Trello webhook does not register:

- `TRELLO_CALLBACK_URL` must be publicly reachable by Trello.
- The service must answer `HEAD /webhooks/trello`.
- Check your Trello key, token, and board ID.

Card enters To Do but nothing runs:

- Check `/dashboard` or `python3 -m taskforge status`.
- Confirm the card has required sections.
- If `TRELLO_START_LABEL_IDS` is set, confirm the card has one of those label IDs.
- Confirm the card is in `TRELLO_TODO_LIST_ID`, not just a list with the same name.

Codex starts but asks a question:

- Read the Trello comment.
- Update the card with the missing detail.
- Move it back to To Do or run `python3 -m taskforge run-card <card-id>`.

PR creation fails:

- Confirm `ENABLE_GIT_PUSH=true`.
- Confirm local `git push` works for `REMOTE_NAME`.
- Confirm `GITHUB_TOKEN` can create PRs for `GITHUB_REPO`.

## Development

Run tests:

```bash
python3 -m unittest discover
```

Run syntax checks:

```bash
python3 -m py_compile taskforge/*.py tests/*.py
```

The suite covers webhook handling, event parsing, card validation, SQLite state,
job queue behavior, worker processing, processor outcomes, runner behavior,
Trello updates, config parsing, CLI commands, and cleanup.
