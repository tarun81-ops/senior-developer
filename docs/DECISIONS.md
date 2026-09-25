# DECISIONS

Architecture decisions and the reasoning behind them. Anything here was
reviewed and approved before implementation started. Dates are 2026.

## D1 — Desktop app with Electron (approved)

**Decision:** ship as an Electron desktop app wrapping a React/Vite UI and a
local FastAPI backend.

**Why:** the product is a local dev tool — it must run `npm`, `pytest` and
write into `workspace/` on the developer's machine. A pure web app cannot do
that without installing a local agent anyway, at which point the desktop shell
is the simpler product. Electron also gives us a bundled Node runtime,
auto-update hooks and a place to store API keys outside the browser.

**Alternatives rejected:** browser-only (no local file/shell access), Tauri
(smaller binary, but adds a Rust toolchain to a Python+JS project).

## D2 — Custom orchestrator, no agent framework (approved)

**Decision:** write our own orchestrator (~a few hundred lines) instead of
adopting LangGraph / CrewAI / AutoGen.

**Why:** our hard requirement is *free-tier survival* — quota accounting per
`provider:model`, Pacific-midnight resets, circuit breakers, budget stops that
need a human, and an event log we fully control for the UI. Frameworks bring
their own abstractions that we would fight, plus fast-moving dependencies, and
none of them model free-tier quotas as a first-class concern. Our domain
complexity is in the provider layer, not in agent plumbing.

**Cost accepted:** we implement the task board / hand-off protocol ourselves
(Phase 2). That code is small and we own it.

## D3 — React + Vite frontend, FastAPI backend (approved)

**Decision:** React with Vite for the UI, FastAPI for the local API.

**Why:** both are explicitly wanted; FastAPI gives typed request/response
models shared conceptually with our Pydantic provider schemas, and Vite keeps
the frontend toolchain boring. The backend already exists as
`backend/core` — Phase 4 only adds an HTTP layer on top of `Runtime`.

## D4 — One HTTP code path: everything speaks OpenAI's wire format (verified 2026-09)

**Decision:** implement a single `OpenAICompatClient` and add providers by
YAML, not by code.

**Why:** Gemini (OpenAI-compatible endpoint), Groq and OpenRouter all accept
`POST /chat/completions` with the same request/response shape. This was
verified against the live docs before building. Consequence: a new provider is
a `config/providers.yaml` entry; retries, failover, quota tracking and event
logging work for it automatically.

## D5 — Free-tier roles: Gemini workhorse, Groq burst, OpenRouter reviewer (approved)

Verified reality (Sept 2026):

| Provider | Free tier | Role |
|---|---|---|
| Gemini Flash | ~1500 req/day, ~1M tokens/min, resets midnight **Pacific**; free prompts may be used for training | planner / architect / coder / devops / docs |
| Groq | ~8K tokens/min, ~200K tokens/day — burst only | tester (short, fast, deterministic) |
| OpenRouter | ~50 req/day on free plan; many `:free` model ids | reviewer / tertiary fallback |

**Why this split:** capacity goes where outputs are largest (Gemini), latency
where responses are smallest (Groq), and the *reviewer deliberately runs a
different model family* — a model reviewing its own output is biased towards
"looks good". OpenRouter's breadth also gives us an escape hatch when Gemini
is out of quota.

**Mitigations baked in:** local quota ledger, ordered fallback chains,
circuit-breaker cooldowns, per-run budgets, and offline mocks so nothing is
blocked on having keys.

## D6 — Config is YAML, logic is code (approved)

**Decision:** `config/providers.yaml`, `config/agents.yaml`, `config/limits.yaml`
hold every tunable: models, limits, routing chains, retries, cooldowns,
budgets. Python reads them into Pydantic models at startup.

**Why:** the whole project is about adapting to provider churn (model ids
retire, quotas change). Tuning must not require touching code, and `doctor`
can validate config without a deploy loop.

## D7 — Event log (JSONL) is the UI contract (approved)

**Decision:** every meaningful step emits a structured event
(`agent.start`, `provider.attempt`, `provider.retry`, `provider.failover`,
`provider.rate_limited`, `llm.response`, `agent.end`, …) to
`data/runs/<run_id>/events.jsonl`. The CLI prints the same events live.

**Why:** Phase 4's desktop UI must show what the system is doing *while* it
works. Reading a log file (later: an API streaming it) means the terminal, the
tests and the UI all see one truth, and a run can be replayed offline for
debugging or demos.

## D8 — Budgets stop for a human (approved)

**Decision:** each run tracks calls and tokens against `config/limits.yaml`;
exceeding the budget raises `BudgetExceeded` and the run ends with exit code 3.

**Why:** on a free tier the natural failure mode is silently burning the
entire daily quota on one broken loop. A budget stop is a feature: a person
decides whether the limit was wrong or the agent is stuck.

## D9 — Mock providers inside the HTTP client (decision)

**Decision:** `mock` and `mock_flaky` are ordinary providers whose "HTTP
response" is simulated in-process (configurable failure kind, count, and
`Retry-After`).

**Why:** retry/backoff/failover/cooldown logic is where the bugs are, and it
must be testable offline, deterministically and for free. `python -m
backend.cli demo` shows the entire failure story with zero keys — the same
code path that later handles real 429s from Gemini.

## D10 — Agents may run commands, but only inside `workspace/` (approved)

**Decision:** generated projects and any `npm` / `pytest` / `git` commands
belong to Phase 3 and are confined to `workspace/<project>/`.

**Why:** the value proposition is production-ready apps, which means running
the tests the agent wrote. Confining execution keeps the blast radius to a
throwaway folder, and every run is recorded in its event log.

## D11 — Deployment: Vercel + Render (approved, Phase 5)

**Decision:** React frontend on Vercel, FastAPI backend on Render.

**Why:** both have generous free/hobby tiers and first-class Vite/FastAPI
support. Note the constraint this creates: the deployed backend cannot run
`npm` on the user's machine, so deployment targets the *generated app*, while
heavy agent runs happen locally.

## D12 — Secrets: env file first, keyring second (decision)

**Decision:** keys are read from `.env` (git-ignored) with an optional Windows
Credential Manager fallback in `backend/core/secrets.py`. `.env.example`
documents every variable. Keys are never printed by the CLI.

**Why:** `.env` is the workflow every dev knows; the keyring keeps secrets out
of the filesystem for users who want that; `doctor`/`providers` only ever show
`set` / `missing`.

**Precedence (fixed in Phase 1):** `.env` wins over the shell environment
(`load_env(override=True)`). A revoked `OPENROUTER_API_KEY` exported at
process level once shadowed a fresh key in `.env`, and editing `.env`
appeared to do nothing — the exact bug class that fix exists to prevent.
CI setups that keep secrets only in the process environment are unaffected
(no `.env` file → nothing is touched).

---

## D13 — Shared task board is a JSON file, not a framework (Phase 2)

**Decision:** stage outputs and hand-offs live on a `TaskBoard` persisted to
`data/runs/<run_id>/board.json` (atomic write, same pattern as `budget.json`).

**Why:** D2 committed us to owning the hand-off protocol. A file is the
simplest thing that serves all three consumers — the orchestrator (context for
the next stage), the CLI (final table), the Phase 4 UI (live state) — and it
survives crashes for free: the last flushed state *is* the truth. Each stage
declares in code which predecessors' artifacts it needs; nothing is passed
implicitly.

## D14 — Review gate: fix loop with a hard stop, tolerant verdict parsing (Phase 2)

**Decision:** the reviewer emits `approve` / `changes_requested`. On
`changes_requested` the coder re-runs with the feedback and the reviewer
re-checks, at most `pipeline.max_fix_iterations` times (default 1). If the
reviewer still objects, the pipeline stops **before** devops/docs and the CLI
exits with a new code `4` — a human decides. Unparseable or unknown-format
reviewer output is treated as approval-with-a-note on the board.

**Why:** the gate must be able to say no (rubber-stamping defeats D5's
different-family reviewer), but a free model's formatting quirk must never
deadlock the run — a stuck pipeline and a broken JSON fence look identical to
a retry loop, and the fix loop alone can burn a day of quota. Stop-forever
only for real disagreement; tolerate-forever with a visible note for format
noise. Exit code 4 keeps "needs your judgement" distinguishable from
"configuration wrong" (1), "providers dead" (2) and "budget hit" (3).

**Amendment (found by running it live):** the first-choice reviewer
(`openrouter/qwen/qwen3.8-27b:free`, a "thinking" model) once returned HTTP 200
with **no content at all** — 4096 output tokens and 108 s spent on internal
reasoning, an empty `content` field. Phase 2 originally accepted that as
"unparseable, therefore approval", which is exactly the silent fake-approval
D14 exists to prevent. The router now has an `empty` failure kind: count the
request and its tokens (they were really spent), put the model on a short
45 s cooldown, and **fail over immediately** (no retry — the failure is the
model's output budget, not a transient error). If every provider returns
empty, the stage fails like any other provider outage. A reviewer that cannot
produce a verdict must never look like one that approved.

---

## D15 — Generated code runs, but only inside `workspace/` (Phase 3)

**Decision:** stage manifests are written into `workspace/<project>/` and commands
are executed there only. Writes go through one sandbox that rejects absolute
paths, `..` segments, NUL bytes and Windows device names, slugifies the project
name, and re-resolves the final path to prove it is inside the project.
Execution uses `shell=False` with an executable allowlist
(`execution.allow` in `config/limits.yaml`), a timeout that kills the process
tree, `cwd` pinned to the project, and head+tail truncated capture.

**Why:** prompts are untrusted input and this tool writes to the user's real
disk and runs real processes. Path validation catches the common escapes;
`shell=False` means `&&`, `|`, `>` and backticks are literal characters, so
injection has nothing to inject into; the allowlist stops a model from invoking
a shell, `curl` or `rm` *at all* — the failure mode is a refusal message, not a
damaged machine. Timeout and truncation exist because free models happily emit
servers, input prompts and 50 MB logs, and a hung pipeline is worse than a
failed one.

**Cost accepted:** some legitimate commands are refused until the user adds them
to the allowlist, and `npm install`/`pip install` are deliberately *not*
run — installing from a model-generated manifest is a supply-chain decision the
human should make, not the orchestrator.

## D16 — Real test output is evidence; it also gates the run (Phase 3)

**Decision:** after the tester finishes, its `run_command` is actually executed
(never the coder's, never guessed when one was named), the result is stored on
the board as `executions`, and a failure triggers a coder fix round exactly like
`changes_requested` does. The output is injected as `### execution` into the
coder's fix context and the reviewer's context. If tests still fail after the
fix loop, the pipeline stops before devops/docs and exits `5`.

**Why:** before this phase the reviewer only saw *claims* in summaries — a model
can describe passing tests that were never run, and we saw the failure mode
live (an empty review being read as approval, D14). Executing the tester's own
command turns "the code looks right" into "exit 0 in 1.5 s". Feeding the real
output back means the fix round is aimed at an observed failure instead of a
guess, which is the single biggest quality jump per token spent. Exit `5` keeps
"your code does not run" distinguishable from "the reviewer objects" (4) and
"the providers are down" (2), and `--no-run-tests` plus the zero-cost `run`
command keep the human in control of when execution happens.

---

## D17 — The API is loopback-only and token-gated on every request (Phase 4)

**Decision:** the FastAPI server binds `127.0.0.1` only (not a default — there is
no flag to change it), and every `/api` route requires three independent checks:
a **per-launch random token** in `X-API-Key`, a **Host header** that names a
loopback address, and an **Origin** from a fixed allowlist (Vite dev servers on
5173 and Electron's `file://`). CORS is configured to exactly those origins, and
request bodies are capped at 256 KiB. The token is generated with `secrets` at
process start, printed once for the desktop shell, and never written to disk or
read from the environment. Approve/reject must additionally re-check the token
at the call site so the requirement is visible in the code.

**Why:** this server runs model-authored code and exposes an "approve this
command" action. Loopback binding is not authentication — any other process on
the machine, including a browser page, can reach `127.0.0.1:8765`. The token
stops the random web page (it cannot read the token), the Host check stops DNS
rebinding (a hostile name resolving to loopback), and the Origin allowlist stops
cross-origin reads in the browser. Three cheap checks beat one clever one, and
none of them is a substitute for the sandbox (D15).

**Cost accepted:** a browser-based UI must be served from the allowlisted
origins, and there is no "open this in any browser tab" convenience. The CLI
keeps working without a token — this decision is about the HTTP surface only.

## D18 — All API state goes through a repository interface over `data/app.db` (Phase 4)

**Decision:** every SQL statement lives behind a `Protocol`
(`EventRepository`) in `backend/api/repository.py`; FastAPI routes and the
pipeline manager only see the interface. SQLite lives at `Settings.db_path`
(`data/app.db`), in WAL mode, with short-lived per-operation connections and
`INSERT OR IGNORE` on `(run_id, seq)` so replay is idempotent. **API keys are
never written to the database** — they go to the git-ignored `.env` (or
Windows Credential Manager), and no endpoint ever returns one. Settings
overrides (model per agent, provider order) persist to a git-ignored local YAML
file written atomically; tracked `config/*.yaml` is never edited.

**Why:** a per-launch in-memory app would lose every event on restart, and SQL
scattered through route handlers would make a future cloud database a rewrite
of the API instead of one new class. Key separation is a security boundary, not
a storage detail: a database backup, a `data/` sync or a stray SQL dump must not
be able to leak a provider key.

**Cost accepted:** SQLite is single-host, so remote/multi-user access is out of
scope until a cloud repository is written; the repository interface is the
seam for that move.

---

## D19 — The UI streams events with `fetch()`, not `EventSource` (Phase 4)

**Decision:** the live event stream is a Server-Sent Events endpoint, but the
desktop client reads it with `fetch()` + `ReadableStream` and decodes the SSE
frames itself, sending the per-launch token in the `X-API-Key` request header.
`EventSource` is not used anywhere in the UI.

**Why:** the browser `EventSource` API cannot set request headers. D17 requires
a token on *every* request, and the token is the control that makes approving a
command safe. So the only two options were `EventSource` with the token in the
query string (leaks it into server logs, browser history and any proxy log) or
`fetch` with a header. The header is the right place for a secret, and a
hand-decoded SSE reader is about thirty lines in the UI.

**Consequences for step 5, now fixed in the design:** the stream endpoint
answers `text/event-stream` and the client must (a) pass `X-API-Key`, (b) pass
`after_seq` to resume, (c) append a timestamped comment frame every ~20s to keep
the connection warm, and (d) reconnect with exponential backoff, resuming from
the last `seq` it saw. A `Last-Event-ID` header is honoured as an equivalent of
`after_seq` so the stream behaves like a standard SSE endpoint for any future
non-UI consumer.

**Cost accepted:** no automatic reconnection and no `Last-Event-ID` handling
from the browser, so the client owns that loop. That is deliberate: the
reconnect policy has to be the same policy the run manager uses when a run dies.

---

## D20 — Background runs are serialized; the lock is released in `finally` (Phase 4)

**Decision:** the API runs at most **one** pipeline at a time. `RunManager`
holds a process-wide lock; a second `POST /api/runs` while one is active is
accepted and queued, and runs execute in submission order. The lock is released
in a `finally` block, so success, failure, cancellation and an unhandled
exception all release it.

**Why:** `ProviderRouter` owns a rate-limit ledger, a cooldown table and a
`QuotaLedger` file, and the registry/router pair is not safe to drive from two
threads at once — the CLI builds one Runtime per invocation and gets a clean
process. A desktop app is long-lived and can be asked for a second run while
the first is still thinking, so the API has to make that safe. Serializing also
keeps the free-tier quota ledger honest, since two concurrent runs would race
on the same daily counters.

**Cost accepted:** the UI shows a queue and a single active run; parallel runs
are not available. If a run hangs, the next one waits — the cancel endpoint
exists precisely so a human can break that. A future multi-run API needs a
lock per provider, not per process, and that is a Phase 5 concern.

---

## D21 — Approval gates pause the run; reject fails it, cancel wins (Phase 4)

**Decision:** a run may ask for human approval at three gates: `plan` (after
the planner), `architecture` (after the architect) and `execution` (before
every command runs, showing the exact command, working directory and timeout).
The worker thread blocks on a `threading.Event` while the run's status is
`waiting_approval`; `POST /api/runs/{id}/approve` and `/reject` resolve it, with
the token re-checked at the call site (D17) and `409` when the run is not
waiting. A waiting run **keeps the single active slot** (D20): nobody else can
run until a human decides, and cancel is the escape hatch. Reject ends the run
`failed` with `reason: "rejected"` and the human's note as the error text;
cancel sets the same event a decision would, so it wakes the gate immediately
(no polling) and wins any race with approve. The CLI never configures gates —
it injects no hook, so it never pauses.

**Why:** D17 promises an "approve this command" action that cannot be forged
from a random page; this is where that promise is kept. Blocking the worker
thread instead of descheduling the pipeline keeps the board, the event log and
one runtime per run exactly as they are — a pause costs nothing and cannot
half-run a stage.

**Cost accepted:** a run left at a gate blocks the queue until approved,
rejected or cancelled (as in D20, the cancel endpoint is the human's lever),
and the execution gate re-opens for every command, including fix-loop reruns.

---

## Phase plan

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Research, provider reality check, architecture decisions | done |
| 1 | Repo scaffold, config layer, provider layer (router, retries, quota, budgets, events), CLI, tests | done |
| 2 | Specialist agents (planner, architect, coder, tester, reviewer, devops, docs) + orchestrator with shared task board | done |
| 3 | Workspace execution: generate files, run tests/builds, iterate | done |
| 4 | FastAPI backend + Electron/React desktop UI | in progress (Part A, step 3 of 6: approval gates) |
| 5 | Deploy generated apps (Vercel + Render) | not started |

