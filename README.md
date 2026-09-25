# senior-developer-agents

A multi-agent software engineering system: you describe an app in plain English,
an orchestrator routes the work to specialist agents (planner, architect,
coder, tester, reviewer, devops, docs), and they build it for you.

It runs **exclusively on free-tier LLM APIs** (Gemini, Groq, OpenRouter) and is
built so that hitting a free-tier limit is a normal, handled event — not a
crash.

> **Current state: Phases 1–3 complete, Phase 4 (API) in progress.**
> The provider layer (routing, retries, failover, quota tracking, budgets,
> event log) runs the seven specialist agents as a pipeline behind
> `python -m backend.cli build`: plan → design → code → test → review
> (with a fix loop) → devops → docs, recorded on a shared task board.
> **Phase 3 executes the result for real**: the stages' files are written into
> `workspace/<project>/`, the tester's own command is run there, and failing
> tests drive another coder round just like a review objection.
> **Phase 4 wraps that pipeline in a local FastAPI server** for the Electron
> desktop UI (see [Local API](#local-api-phase-4-part-a--done) and
> [Desktop app](#desktop-app-phase-4-part-b--in-progress)). The API is done;
> the desktop UI is being built in reviewed phases.

---

## Quickstart (Windows / PowerShell)

Easiest path — the setup script creates the venv, installs dependencies,
copies `.env.example` to `.env` and runs the checks:

```powershell
.\scripts\setup.ps1
```

Or by hand:

```powershell
# 1. Python 3.11+ (3.12/3.13 recommended; 3.14 works but is very new)
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# 2. keys (all three are optional to start — mocks work with none)
Copy-Item .env.example .env
notepad .env        # paste your keys:
                    #   GEMINI_API_KEY     https://aistudio.google.com/apikey
                    #   GROQ_API_KEY       https://console.groq.com/keys
                    #   OPENROUTER_API_KEY https://openrouter.ai/keys

# 3. is everything wired correctly?
.\.venv\Scripts\python -m backend.cli doctor

# 4. the whole retry/failover story, offline and free
.\.venv\Scripts\python -m backend.cli demo
```

From here on, `python -m backend.cli` means
`.\.venv\Scripts\python -m backend.cli`.

---

## Commands

| Command | What it does |
|---|---|
| `doctor` | Checks Python, config files, folders, API keys, cooldowns, and runs an offline end-to-end request. Non-zero exit only on real problems. |
| `providers` | Providers, where each key comes from, models configured, quota used today, active cooldowns. |
| `providers --clear-cooldowns` | Forgets circuit-breaker cooldowns. Run this after fixing a key, otherwise the provider stays blocked for minutes. |
| `models --provider groq --live` | Models from config, or from the provider itself with `--live`. Add `--free-only` for `:free` OpenRouter ids. |
| `ask "build me a todo app"` | One prompt to one agent: live event log, answer, routing report. |
| `ask "..." --provider mock --json` | Machine-readable output. Also `--quiet`, `--agent`, `--model`, `--temperature`, `--max-tokens`, `--fast`. |
| `build "a pomodoro timer CLI"` | **The pipeline:** planner → architect → coder → tester → reviewer → (fix loop) → devops → docs over a shared task board. Live stage events, then a board table, verdict and budget. |
| `build "..." --stages planner,architect` | Run only part of the pipeline. Also `--provider` (force one provider), `--json`, `--quiet`, `--fast`. |
| `build "..." --project myapp` | Name the workspace folder (default: a slug of the goal, so re-runs land in the same project). `--no-apply` skips writing files, `--no-run-tests` skips execution, `--dry-run` reports what would be written and writes nothing. |
| `run [--project myapp]` | **Re-run a generated project's tests with no model calls and no quota used.** Uses the tester's recorded `run_command` (or `--command`, or auto-detection). Exit `0` pass, `5` failing. |
| `demo` | Offline demonstration: first provider always answers 429, second succeeds — watch retry → backoff → cooldown → failover → success. |
| `events -n 25` | Tails the JSONL event log of the most recent run (`--kind`, `--run-id` to filter). |

Exit codes: `0` ok · `1` configuration/setup problem · `2` runtime failure
(all providers failed) · `3` run budget exhausted (needs a human) ·
`4` pipeline review still says `changes_requested` after the fix loop (needs a human) ·
`5` the project's tests are still failing after the fix loop (needs a human).

---

## How it works

```
config/providers.yaml   who can we call? (base URLs, keys, limits, models)
config/agents.yaml      which agent uses which model, in what fallback order
config/limits.yaml      retries, backoff, cooldowns, budgets, request timeouts
        │
        ▼
   Registry ──► ProviderRouter ──► OpenAICompatClient ──► Gemini / Groq / OpenRouter
        │              │              (all three speak the OpenAI wire format,
        │              │               so there is exactly one HTTP code path)
        │              ├─► QuotaLedger   data/quota.json            RPM/RPD/TPM + cooldowns
        │              ├─► BudgetTracker data/runs/<id>/budget.json
        │              └─► EventBus      data/runs/<id>/events.jsonl
        ▼
      Agent (prompt file + routing chain) ──► CLI today, FastAPI + Electron UI in Phase 4
```

Key behaviours, all covered by tests:

- **Failover is config, not code.** Each agent has an ordered routing chain.
  A provider that is rate-limited, out of daily quota, cooling down after a
  failure, or missing a key is skipped and the router moves to the next one.
  Adding a provider = a YAML edit.
- **Retries with real backoff.** Exponential + jitter, honours an explicit
  `Retry-After`, stops after `max_attempts_per_provider`.
- **Circuit breaker.** Auth/429/5xx failures put a provider on cooldown so we
  stop hammering a broken provider; `providers` shows it, `doctor` warns about
  it, `--clear-cooldowns` clears it.
- **Offline mocks are built in.** `mock` always succeeds, `mock_flaky` always
  answers 429/401/500 on demand — so retry, failover and budget logic can be
  demonstrated and tested with zero keys and zero cost.
- **Event log is the contract.** Every step emits a structured event to
  `data/runs/<run_id>/events.jsonl`. The CLI prints them live; the Phase 4 UI
  reads the same file, so what you see in the terminal is exactly what the UI
  will replay.
- **Budget guard.** Each run counts calls and tokens against
  `config/limits.yaml`; exceeding it raises `BudgetExceeded` and stops for a
  human instead of silently burning free quota.
- **The pipeline is config, too.** Stage order and the fix-loop limit live in
  `config/agents.yaml` under `pipeline:`; each stage's model comes from its
  agent entry. Changing the process is a YAML edit. Stage outputs are written
  to `data/runs/<run_id>/board.json` after every transition, so the next stage
  (and the Phase 4 UI) always know exactly where the run is.
- **The reviewer can say no — once per fix round, then it stops.**
  `changes_requested` re-runs coder with the feedback, bounded by
  `max_fix_iterations`; if the reviewer still objects the pipeline stops with
  exit code 4 for a human. Unparseable reviewer output is treated as approval
  with a note on the board, because a formatting quirk must never deadlock a
  run (D13, D14).
- **Generated code actually runs — inside a sandbox.** Stage manifests are
  written to `workspace/<project>/` (atomic writes, `..`/absolute/device paths
  rejected, project names slugified), then the tester's `run_command` is
  executed there with `shell=False`, an executable allowlist
  (`config/limits.yaml` → `execution.allow`), a timeout that kills the process
  tree, and head+tail truncated output. Real failures drive another coder
  round; if they survive, the run stops with exit code 5. `python` is pinned to
  the interpreter running the CLI, so the generated tests see the same
  environment (D15, D16).

### Free-tier model assignment

| Role | Provider | Why |
|---|---|---|
| planner / architect / coder | Gemini Flash | Workhorse: ~1500 req/day, 1M tokens/min, long context |
| tester | Groq (`openai/gpt-oss-120b`) | Very fast, short deterministic outputs (burst only: 8K TPM) |
| reviewer | OpenRouter `qwen/qwen3.8-27b:free` | A **different** model family on purpose — a model grading its own work says "looks good" too often |
| devops / docs | Gemini Flash-Lite | Simple, high-volume tasks |

Model ids drift. Refresh them with:

```powershell
python -m backend.cli models --provider openrouter --live --free-only
```

and update `config/providers.yaml` / `config/agents.yaml` if an id 404s.

---

## Local API (Phase 4, Part A — done)

The same pipeline, over HTTP, for the desktop UI (Part B, below). Part A
is complete: foundations, run queue, approval gates, settings, the SSE stream,
and end-to-end tests that drive a whole gated run the way the UI will.

```powershell
.\.venv\Scripts\python -m backend.api --port 8765
```

It prints a **token that is regenerated on every launch** — the UI reads it from
that line and sends it as `X-API-Key` on every request:

```powershell
# health check (token required)
Invoke-RestMethod http://127.0.0.1:8765/api/health -Headers @{ "X-API-Key" = "<token>" }

# start a run that pauses for approval before any command runs
$h = @{ "X-API-Key" = "<token>" }
$run = Invoke-RestMethod http://127.0.0.1:8765/api/runs -Method Post -Headers $h `
  -ContentType application/json -Body '{"request": "a CLI todo app", "approval_gates": ["execution"]}'

# watch it live (curl.exe -N disables buffering; the stream ends with the run)
curl.exe -N -H "X-API-Key: <token>" "http://127.0.0.1:8765/api/events/stream?run_id=$($run.run_id)"

# when it reports waiting_approval, look at the gate, then decide
Invoke-RestMethod "http://127.0.0.1:8765/api/runs/$($run.run_id)" -Headers $h
Invoke-RestMethod "http://127.0.0.1:8765/api/runs/$($run.run_id)/approve" -Method Post -Headers $h
```

`--port 0` lets the OS pick a free port; `GET /api/health` reports the one
actually bound. There is no `--host` flag: the bind is always `127.0.0.1`.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness + the port actually bound. |
| `POST /api/runs` | Queue a run (`202`): `{"request": "...", "project"?, "stages"?, "approval_gates"?: ["plan", "architecture", "execution"], "provider"?, "model"?, "override"?, "dry_run"?, "no_apply"?, "no_run_tests"?}`. One run executes at a time; the rest wait in FIFO order (D20). |
| `GET /api/runs?limit=` | Runs from this process, newest first, plus the active run id. |
| `GET /api/runs/{run_id}` | Full state: status, queue position, open gate and its payload, board, agent outputs, files written, test results, budget. |
| `POST /api/runs/{run_id}/cancel` | Cancel: a queued run never starts; a running one stops at the next checkpoint (a running command's process tree is killed); a gate wakes immediately. |
| `POST /api/runs/{run_id}/approve` | Approve the gate the run is waiting at (`409` if it isn't waiting). Optional `{"note": "..."}`. |
| `POST /api/runs/{run_id}/reject` | Reject it: the run ends `failed` with reason `rejected` and the note as its error (D21). |
| `GET /api/projects` | Project folders under `workspace/`: names, file counts, timestamps; never paths outside it or file contents. |
| `GET /api/events?run_id=&after_seq=` | Replay persisted events (D7 contract, stored in SQLite). |
| `GET /api/events/stream?run_id=&after_seq=` | Live SSE (`text/event-stream`): replays events after the cursor, then streams new ones, then closes after the run's `api.run_succeeded` / `api.run_failed` / `api.run_cancelled` event. Each frame's `id:` is its `seq`; reconnect with the last one as `after_seq` (or `Last-Event-ID`) for no gap and no repeat. Read it with `fetch()`, not `EventSource`, so `X-API-Key` is sent (D19, D23). |
| `GET /api/settings` | Effective model chain per agent, providers and their models, provider order, and each expected key as `set` / `missing` (never a value). |
| `PUT /api/settings` | Replace the local overrides: `{"agents": {"coder": {"provider": "groq", "model": "..."}}, "provider_order": ["groq", "gemini"]}`. Unknown names are `422`. `{}` resets to the tracked defaults. Applies from the next run (D22). |
| `PUT /api/settings/keys` | Write-only: `{"keys": {"GEMINI_API_KEY": "..."}}` goes to `.env`; the response reports `set` / `missing` only. |

Every route above needs the token; a test enumerates the whole surface and
fails if a route is added without being listed, tested and documented.

Security is deliberately boring and layered (D17): the server binds
`127.0.0.1` only, every request must present the per-launch token, the `Host`
header must be loopback (blocks DNS rebinding), `Origin` must be exactly a Vite
dev server (`http://127.0.0.1:5173`, `http://localhost:5173`) or the packaged
desktop UI's `sda://app`, and bodies over 256 KiB are refused. `sda:` is a
custom protocol the Electron shell (Part B) registers as standard + secure +
fetch/CORS-enabled and serves the built UI from, because a `file://` page sends
`Origin: null`, which can't be told apart from any sandboxed iframe. The same
list drives CORS, so the UI's preflights pass. Events are
persisted to `data/app.db` through a repository interface (D18) so a cloud
database can replace SQLite later; **API keys are never stored in the database**
and are never returned by any endpoint.

---

## Desktop app (Phase 4, Part B — in progress)

`desktop/` is the Electron shell plus the React/Vite UI. It is built in
phases, each ending with something you can run:

| Phase | What you get | Status |
|---|---|---|
| B1 | App shell: backend launch, token handoff, `sda://app` serving, health shown in the window | **done** |
| B2 | Create a run + live event timeline | **done** |
| B3 | Approval dialogs (plan/architecture as sanitized markdown, execution shows the exact command/cwd/timeout) | next |
| B4 | Task board + read-only file viewer | |
| B5 | Settings (model per agent, keys write-only, ignored-overrides warning) | |
| B6 | Run history + polish | |

Install and run (PowerShell, from the repo root; Node 20+ and the `.venv`
from the Quickstart):

```powershell
cd desktop; npm install
```

```powershell
npm start          # build the UI and open the app
npm run dev        # development: Vite hot reload + Electron
npm test           # client, form rules, bundle check, shell vs a real backend (offline)
npm run smoke      # real Electron: security checks, then drives two offline mock
                   # runs through the UI (one to success, one cancelled at a gate)
```

What B2 gives you:

- **Starting a run.** The **New run** form takes a request, an optional
  project folder name, and which gates to stop at. The execution gate starts
  ticked on every new form (D27).
- **Following it live.** The run appears next to the form with its status, a
  stage timeline (planner through docs) and a live event log. The log is read
  from the stream with `fetch()`, and it resumes from the last event after a
  dropped connection.
- **Gates and cancel.** A run that reaches a gate says which one it is
  waiting at; the approval dialogs are B3. **Cancel run** works at any point
  before the run finishes.
- **Developer menu.** In development builds (`npm run dev`) a **Developer**
  menu offers the offline mock model, so you can try runs without spending
  quota. Production builds (`npm start`) don't contain it, and a test builds
  the production bundle to prove that.

How it fits together (D25, D26):

- **Starting the backend.** The shell generates the per-launch token itself
  and starts `python -m backend.api --port 0 --token-stdin --exit-with-stdin`
  from `.venv` (override the interpreter with `SDA_PYTHON`).
  - The token goes in as the first line of the backend's stdin. It is never
    in argv, the environment, a file or any output.
  - The backend answers with one line, `SDA_READY {"port": N}`, which is the
    only thing the shell reads from its stdout.
  - If the backend exits or stays silent before that line, the app reports
    the error with the end of its stderr instead of hanging. If the backend
    dies later, the app reports that too.
- **Shutting down.** Closing the app closes the backend's stdin, so the API
  shuts down gracefully and stops any running command. It also exits by
  itself if the app crashes.
- **Serving the UI.** It is served from `sda://app` (Vite on `127.0.0.1:5173`
  in development).
- **What the page can do.** Context isolation and the sandbox are on, and Node
  integration is off. A strict CSP limits the page to its own files and the
  API's address. The page's only bridge is `sda.connection()`, which returns
  `{ baseUrl, token }` and nothing else.

---

## Project layout

```
config/                 providers.yaml, agents.yaml, limits.yaml (all config lives here)
backend/
  cli.py                doctor / providers / models / ask / build / demo / events
  api/                  FastAPI layer for the desktop UI (Phase 4)
    app.py              app factory, middleware, routers
    security.py         per-launch token, Host/Origin checks, body cap (D17)
    repository.py       EventRepository protocol + SQLite implementation (D18)
    event_store.py      EventBus -> repository bridge, sequence numbers, replay
    models.py           Pydantic request/response models
    stream.py           SSE stream: replay from a cursor, then live, then close (D23)
    run_manager.py      FIFO run queue, cancel, approval gates (D20, D21)
    settings_service.py settings snapshot, override validation, write-only keys (D22)
    __main__.py         python -m backend.api (binds 127.0.0.1, prints the token)
  core/
    config.py           Settings + .env loading
    errors.py           error hierarchy (retryable vs not)
    secrets.py          env + Windows Credential Manager key lookup
    runtime.py          composition root (the one place that wires everything)
    provider/           schemas, registry, quota ledger, HTTP client, router
    agents/             Agent base class, JSON extractor, prompts/*.md
    events/             EventBus + JSONL writer
    orchestrator/       BudgetTracker, TaskBoard (board.json), Pipeline (stage runner)
    workspace/          sandbox paths, apply (board -> files), CommandRunner
  tests/                244 tests, no network, no keys required
desktop/                Electron shell + React/Vite UI (Phase 4, Part B)
  electron/main.cjs     sda://app protocol, window lockdown, CSP, --smoke
  electron/backend.cjs  token generation, backend launch, SDA_READY, graceful stop
  electron/preload.cjs  the page's only bridge: connection() -> { baseUrl, token }
  electron/smoke-page.js  what `npm run smoke` does inside the real page
  src/api.js            API client + fetch()-based SSE reader (resume, backoff, dedupe)
  src/runRequest.js     new-run defaults (execution gate on) and request body
  src/NewRunForm.jsx    the New run form; DevMenu.jsx is development-only
  src/RunView.jsx       status, stage timeline, live event log, cancel
  test/                 node --test: client, form rules, bundle, shell vs a real backend
docs/DECISIONS.md       why each decision was made (D1–D28)
scripts/setup.ps1       one-shot Windows setup
data/                   runtime state (quota, runs, events) — git-ignored
workspace/              where generated apps will live — git-ignored
```

---

## Tests

```powershell
.\.venv\Scripts\python -m pytest          # 244 tests, ~50s, offline
```

```powershell
cd desktop; npm test                       # 16 tests: stream client, form rules, production
                                           # bundle has no mock model, shell vs a real backend
npm run smoke                              # real Electron: 16 steps, exits 0/1
```

The suite covers the quota ledger, backoff maths, router failover order, HTTP
error classification (via a fake transport), empty-completion handling, agent
JSON extraction, the event log, the task board, the pipeline (stage order,
context wiring, fix loop, budget stops), the workspace sandbox (path escapes,
apply/dry-run/conflicts, command allowlist, timeouts, output truncation), the
CLI end-to-end against a throwaway project root, and the whole API: token,
Host/Origin, body cap, CORS, event persistence and replay, the run queue,
approval gates, settings, and the SSE stream (replay, live handoff, resume,
close). `test_api_e2e.py` starts a real uvicorn server on an ephemeral loopback
port and drives full gated runs over HTTP with a streaming client, approving,
rejecting and cancelling in reaction to stream events, and checks that no
response ever contains a key value. No test touches the network or needs an
API key.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 User not found.` from OpenRouter | Key revoked/stale — create a new one at openrouter.ai/keys. (Listing models works anyway: `/models` is public and does **not** validate your key.) |
| Provider silently missing from the chain | It is on cooldown or has no key. `python -m backend.cli providers` shows both; `--clear-cooldowns` after fixing a key. |
| `No endpoints found for <model>` / 404 | Model id retired. Re-run `models --live` and update the YAML. |
| Gemini daily quota exhausted | Free tier resets at **midnight Pacific**. The ledger knows this and shows the reset boundary in `providers`. |
| `BUDGET STOP` (exit 3) | A run exceeded its call/token budget. Intentional — review `data/runs/<run_id>/budget.json`, then raise the limit in `config/limits.yaml` if it was correct. |
| A call succeeds but the answer is empty / the reviewer approves without a verdict | Free "thinking" models can spend the whole output budget on internal reasoning and return an empty message. The router treats that as a failure (`empty`), cools the model down 45 s and fails over — the tokens spent are still counted. If it happens often for one agent, raise its `max_output_tokens` in `config/agents.yaml`. |
| Pipeline stops with exit 4 | The reviewer said `changes_requested` and still did after the fix loop. Read `data/runs/<run_id>/board.json` (the `reviewer` record lists the issues), fix by hand or re-run. |
| Pipeline stops with exit 5 | The project's tests still fail after the fix loop. The failing command and its output are in the board (`executions`) — reproduce with `python -m backend.cli run`, or skip execution with `--no-run-tests`. |
| `REFUSED: '<x>' is not on the execution allowlist` | The model named a command we never run (a shell, `curl`, …). Either edit that project's `run_command`, pass `--command`, or add the executable to `execution.allow` in `config/limits.yaml` if you trust it. |
| A generated command hangs | It is killed after `execution.timeout_seconds` (default 300 s) and recorded as `timed_out: true`. Lower the timeout if you want faster feedback. |
| Installing on Python 3.14 fails | Use 3.12 or 3.13: `py -3.12 -m venv .venv`. |

**Never** paste real keys into prompts, and note that Google may use free-tier
Gemini prompts to improve its models — no passwords, private keys or customer
data in prompts.

---

## Roadmap

- **Phase 1 (done)** — provider layer: config-driven routing, retries,
  failover, quota ledger, budgets, event log, CLI, tests.
- **Phase 2 (done)** — specialist agents + orchestrator: planner, architect,
  coder, tester, reviewer, devops, docs; shared task board (`board.json`),
  review fix loop, `build` command, 91 tests.
- **Phase 3 (done)** — workspace execution: manifests written into
  `workspace/<project>/`, the tester's command run in a sandbox (allowlist,
  timeout, truncated capture), test failures feeding the fix loop, `run`
  command for zero-cost re-runs, 138 tests.
- **Phase 4 (in progress)** — FastAPI backend + React/Vite + Electron desktop UI.
  **Part A (the API) is done.** Step 1 done: loopback-bound API with
  per-launch token, Host/Origin checks,
  request-size cap and SQLite-backed event replay (`data/app.db`, D17/D18).
  Steps 2–4 done: FIFO run queue with cancel (D20), approval gates (D21),
  settings with git-ignored local overrides and write-only keys (D22).
  Step 5 done: SSE event stream with cursor resume (D19, D23).
  Step 6 done: end-to-end tests over a real server, security pass, pinned
  API surface (D24).
  **Part B (the desktop UI) is in progress.** B1 done: app shell, backend
  launch with the token over stdin and `SDA_READY`, `sda://app` serving,
  locked-down renderer with CSP, health in the window (D25–D27). B2 done:
  New run form (execution gate on by default), live stage timeline and event
  log, cancel, development-only mock model (D28). Next: B3.
- **Phase 5** — deploy: Vercel (frontend) + Render (backend).


