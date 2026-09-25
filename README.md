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
> desktop UI (see [Local API](#local-api-phase-4--in-progress)); the API layer
> is being built in reviewed steps.

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

## Local API (Phase 4 — in progress)

The same pipeline, over HTTP, for the desktop UI. It is built in reviewed steps;
**steps 1–4 are done** (foundations, run queue, approval gates, settings);
SSE streaming follows.

```powershell
.\.venv\Scripts\python -m backend.api --port 8765
```

It prints a **token that is regenerated on every launch** — the UI reads it from
that line and sends it as `X-API-Key` on every request:

```powershell
# health check (token required)
Invoke-RestMethod http://127.0.0.1:8765/api/health -Headers @{ "X-API-Key" = "<token>" }

# replay a run's events (after_seq is the cursor a reconnecting UI sends back)
Invoke-RestMethod "http://127.0.0.1:8765/api/events?run_id=<run_id>&after_seq=0" `
  -Headers @{ "X-API-Key" = "<token>" }
```

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness + the port actually bound. Token required. |
| `GET /api/events?run_id=&after_seq=` | Replay persisted events (D7 contract, stored in SQLite). |
| `GET /api/settings` | Effective model chain per agent, providers and their models, provider order, and each expected key as `set` / `missing` (never a value). |
| `PUT /api/settings` | Replace the local overrides: `{"agents": {"coder": {"provider": "groq", "model": "..."}}, "provider_order": ["groq", "gemini"]}`. Unknown names are `422`. `{}` resets to the tracked defaults. Applies from the next run (D22). |
| `PUT /api/settings/keys` | Write-only: `{"keys": {"GEMINI_API_KEY": "..."}}` goes to `.env`; the response reports `set` / `missing` only. |

Security is deliberately boring and layered (D17): the server binds
`127.0.0.1` only, every request must present the per-launch token, the `Host`
header must be loopback (blocks DNS rebinding), `Origin` must be a Vite dev
server or Electron's `file://`, and bodies over 256 KiB are refused. Events are
persisted to `data/app.db` through a repository interface (D18) so a cloud
database can replace SQLite later; **API keys are never stored in the database**
and are never returned by any endpoint.

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
  tests/                206 tests, no network, no keys required
docs/DECISIONS.md       why each decision was made (D1–D22)
scripts/setup.ps1       one-shot Windows setup
data/                   runtime state (quota, runs, events) — git-ignored
workspace/              where generated apps will live — git-ignored
```

---

## Tests

```powershell
.\.venv\Scripts\python -m pytest          # 206 tests, ~20s, offline
```

The suite covers the quota ledger, backoff maths, router failover order, HTTP
error classification (via a fake transport), empty-completion handling, agent
JSON extraction, the event log, the task board, the pipeline (stage order,
context wiring, fix loop, budget stops), the workspace sandbox (path escapes,
apply/dry-run/conflicts, command allowlist, timeouts, output truncation), the
CLI end-to-end against a throwaway project root, and the API foundations (token,
Host/Origin, body cap, CORS, SQLite event persistence and replay). No test
touches the network or needs an API key.

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
  Step 1 done: loopback-bound API with per-launch token, Host/Origin checks,
  request-size cap and SQLite-backed event replay (`data/app.db`, D17/D18).
  Steps 2–4 done: FIFO run queue with cancel (D20), approval gates (D21),
  settings with git-ignored local overrides and write-only keys (D22).
  Next: SSE event stream (D19).
- **Phase 5** — deploy: Vercel (frontend) + Render (backend).


