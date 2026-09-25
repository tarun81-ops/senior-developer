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
5173 and the desktop UI's `sda://app`). CORS is configured to exactly those origins, and
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

**Amended (step 4→5):** the packaged UI's origin is `sda://app`, not `file://`.
An Electron page loaded over `file://` sends `Origin: null`, which every
sandboxed iframe and `data:` page also sends, so `null` is refused. `sda`
("senior developer agents") is a custom protocol that Part B registers before
`app.ready` with `protocol.registerSchemesAsPrivileged` (`standard`, `secure`,
`supportFetchAPI`, `corsEnabled`), serving `desktop/dist` from `sda://app/`. A
standard scheme's origin is `scheme://host`, so the renderer sends exactly
`sda://app`. The allowlist matches it exactly, and the Origin check and CORS
share one list: an earlier version accepted any `sda://` host in the check but
left it out of CORS, so every UI preflight (`X-API-Key` always triggers one)
would have been refused. Part B must use this scheme and host verbatim.

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

## D22 — Settings: local overrides layered at load time, keys write-only (Phase 4)

**Decision:** implements the settings half of D18. `GET /api/settings` and
`PUT /api/settings` read and replace two kinds of override: a pinned
first-choice model per agent and a global `provider_order`. They persist to
the git-ignored `data/settings.local.yaml`, written atomically (temp file,
then replace), and `Registry.load` layers them over the tracked
`config/agents.yaml` every time a registry is built. The tracked YAML is
never edited. A pin goes to the front of the agent's chain and the rest of the
configured chain stays behind it as the fallback. `provider_order` only
reorders the chain slots held by the providers it names, so an unnamed
provider keeps its place (the `demo` agent's failing first hop, the offline
mock at the end of every chain). A `PUT` is validated by loading a registry
with the candidate overrides. An unknown agent, provider or model, a
provider listed twice, or an unexpected field is a `422`, and nothing is
written. `{}` clears every override. `PUT /api/settings/keys` writes keys to
`.env` only (D12), replacing an existing line or appending one. It accepts
only the key variables the tracked providers declare, and values made of
characters real keys use. Responses report each expected key as `set` or
`missing`, and a rejected value is never echoed in the `422`.

**Why:** reading overrides inside `Registry.load` is the one place every
consumer already passes through. Each API run builds its own runtime (D20),
so a change reaches the *next* run automatically. A run already in progress
keeps the registry it started with, and nothing mutates it. The CLI gets the
same overrides, so the desktop UI and the terminal never disagree about which
model a stage uses. Pinning instead of replacing the chain keeps free-tier
failover (D5) intact when the chosen model hits a quota. Key values are
checked in the route, not by a Pydantic validator, because FastAPI's
validation error body echoes the submitted input, and that input is the key.

**Cost accepted:** settings are read when a run *starts*, so a run still in
the queue picks up a change made after it was submitted. A stale or corrupt
overrides file (unparsable YAML, or one naming an agent, provider or model
later removed from the tracked config) is **ignored**: registry loads fall
back to the tracked `config/` defaults and log a `WARNING` naming the file,
so a bad override cannot take down runs or the CLI. `GET /api/settings` then
shows the defaults, which are what runs really use. A `PUT` still validates
strictly, and `PUT /api/settings` `{}` or deleting the file clears the
warning. A broken *tracked* config still fails loudly. Keys cannot be cleared
through the API yet, and a key held only in Windows Credential Manager shows
as `set` but is not written there.

---

## D23 — The SSE stream is one cursor over SQLite; the run's closing event ends it (Phase 4)

**Decision:** `GET /api/events/stream?run_id=&after_seq=` implements D19. It
has no separate live channel. The stream runs the replay query
(`seq > cursor`) and sends what it finds, then blocks on the event store's
condition variable (at most 1 s) and runs the query again. The cursor is the
last `seq` actually sent, so replay, the replay-to-live handoff and a
reconnect are all the same query. None of them can drop or repeat an event.
Each frame's SSE `id` is its `seq`, and `after_seq` (or `Last-Event-ID`)
resumes after it. The stream closes after sending the run's closing event
(`api.run_succeeded`, `api.run_failed` or `api.run_cancelled`). A run with no
live source (already finished, or from an earlier process) closes once the
stream has caught up. A timestamped `: keepalive` comment goes out after about
20 s of silence. The route sits under the token-guarded router like every
other.

Two ordering guarantees were added to make "closing event sent" mean "every
event sent":

* the event store numbers and writes an event in one critical section, so two
  threads emitting for one run (worker and a cancel request) can never make
  `seq` N+1 visible before N;
* the run manager writes a run's closing event in the same locked step that
  sets its terminal status, and writes every request-thread event (queued,
  cancel requested, approved/rejected) under that lock too. Nothing can land
  after the closing event. A run cancelled while still queued now gets one
  as well; before, it got none.

**Why:** two sources (a DB replay plus an in-memory live queue) need a merge
step, and that merge is where gaps and duplicates come from. With one
cursor over one ordered table, correctness follows from the primary key. The
cost of waking and re-querying is one indexed read of a sub-millisecond
SQLite table per event batch, for the one or two local clients a desktop app
has.

**Cost accepted:** each open stream holds a thread-pool thread for up to
1 s at a time while waiting. That is fine for a local UI, but a
many-client server would need an async notifier. The event write is now
inside the store's lock, which serialises event writes across runs; with one
active run (D20) that costs nothing.

---

## D24 — The API surface is pinned by a test; e2e runs use a real server (Phase 4)

**Decision:** Part A closes with two test rules. First, a test compares the
app's full route list, generated in process from the OpenAPI schema (the
schema is still never served), to a fixed set of 13 method/path pairs. The
same set drives a parametrised test asserting `401` without the token and with
a wrong one. A new route therefore fails the suite until it is listed, which
puts it under the token test automatically. Second, end-to-end tests run a
real uvicorn server on an ephemeral `127.0.0.1` port and drive it with a
streaming `httpx` client, deciding each gate in reaction to the
`api.run_waiting_approval` event on the stream, as the UI will.

**Why:** the token guard is router-level, so a route mounted outside that
router would be public and nothing would notice. Pinning the surface turns
that silent mistake into a failing test. `TestClient` buffers a streaming
response until the app finishes it, so it can't show that frames reach a
client while the run is still going, or that a client can act on them
mid-stream. A real socket on loopback can, and it still spends no quota and
doesn't touch the network.

**Cost accepted:** adding an endpoint means editing the pinned set, which is
the point. The e2e tests start a server per test (about a second each).

---

## D25 — Desktop shell: the shell makes the token and hands it over stdin (Phase 4, Part B)

**Decision:** the Electron main process generates the per-launch token
(`crypto.randomBytes(32)`, base64url) and starts the backend as
`python -m backend.api --port 0 --token-stdin --exit-with-stdin`.

* **Token.** It is written as the first line of the child's stdin. The
  launcher refuses anything that is not a 43–128 character URL-safe string,
  and prints nothing secret in this mode.
* **Port.** The launcher binds and listens on `127.0.0.1:0` before announcing
  it, then prints exactly one line: `SDA_READY {"port": N}`. That is the only
  thing the shell reads from stdout. After it, stdout is drained unread, and
  the port must be an integer from 1 to 65535.
* **Failure.** If the process cannot start, exits, or stays silent for 30 s
  before `SDA_READY`, startup is rejected immediately with the exit code and
  the end of stderr, and the app shows that error instead of hanging. A
  backend that dies after startup is reported the same way.
* **Lifetime.** stdin stays open as the backend's lifeline. On quit the shell
  closes it; the launcher sees EOF and stops like Ctrl+C would (running
  commands are cancelled and their trees killed, D20). The shell hard-kills
  only after 8 s. If the shell crashes, the pipe closes by itself. This was
  verified by force-killing Electron: the backend exited on its own.
* Run by hand, `python -m backend.api` keeps generating and printing a token
  for curl.

**Why:** the token is the control that makes "approve this command" safe
(D17), so it should be created by the party that needs it and handed over
deliberately, not scraped from a log line. Other processes of the same user
can read argv and environment variables. A pipe between parent and child is
readable only by those two processes. A dynamic port avoids "port in use",
and binding before announcing means a client that connects immediately is
never refused. A stdin lifeline works on Windows, where there is no graceful
signal to send a child, and it also covers a crashed parent.

**Cost accepted:** development needs the repo's `.venv` (or `SDA_PYTHON`).
Bundling Python into an installer is a packaging step that is not designed
yet.

## D26 — Renderer security: two values across the bridge, CSP, nothing else (Phase 4, Part B)

**Decision:**

* **Window.** One window with `contextIsolation: true`, `sandbox: true`,
  `nodeIntegration: false` and `webviewTag: false`. Navigation away from our
  page is blocked, `window.open` is denied, webviews cannot attach, and every
  permission request is denied. The app menu and DevTools exist only in
  development.
* **Bridge.** The preload exposes exactly one function,
  `sda.connection()`, which returns `{ baseUrl, token }`. The IPC handler
  answers only our own top-level page (`sda://app/…`, or the Vite URL in
  development). The renderer never sees paths, Node, `ipcRenderer` or
  anything else.
* **Serving.** `sda://app` serves only files inside `desktop/dist` (other
  hosts, traversal and missing files are a 404). Every response carries
  `X-Content-Type-Options: nosniff` and the CSP
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src <API base URL>; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'`.
  There is no `'unsafe-inline'` anywhere. Styles come from a stylesheet, and
  React's style props go through the CSSOM, which the CSP allows.
* **Checks.** `npm run smoke` runs real Electron and verifies from inside the
  page that:
  * it connected;
  * its origin is `sda://app`;
  * `require` and `process` are absent;
  * `window.sda` has exactly one key;
  * a fetch to any other origin is stopped by `connect-src`. The test uses
    loopback port 1, so nothing real is contacted.

**Why:** the renderer shows model-authored text and holds the button that
approves running a command. Anything that runs script there would have the
token. So the renderer gets the least possible: no Node, a two-value bridge,
and a CSP under which injected markup can neither run script nor send data
anywhere except the API.

**Cost accepted:** no inline styles or scripts, no remote fonts or images,
and no DevTools in the built app. Development uses Vite's server without the
CSP header, because hot reload injects inline styles. The smoke test runs
against the built files, which do get the CSP.

## D27 — UI rules fixed before any screen is built (Phase 4, Part B)

**Decision:** four rules every screen follows:

1. **The execution gate is on by default** for every new run. The new-run
   form starts with it checked every time and never remembers it as off.
2. **The offline mock model is available only in a development-only menu.**
   The menu is compiled into development builds and left out of production
   builds; it never appears in the normal model lists.
3. **Model-written markdown is rendered with a well-known sanitizing
   renderer.** `react-markdown` builds React elements, with no raw-HTML plugin,
   so HTML in model output is dropped. **Links are rendered as plain,
   non-clickable text.** Clickable links, if ever wanted, are a separate,
   deliberately reviewed feature.
4. **Functional before visual.** No theming or animation until the screens
   work.

**Why:** these guard the approval gates. A defaulted-off execution gate, a
mock model picked by accident, or a model-authored link or script shown next
to "Approve" are each a way to get a human to approve something they did not
read.

**Cost accepted:** the new-run form has one checkbox you must untick every
time you want to skip the execution gate, and links in plans must be copied
by hand.

---

## D28 — Run screen: events from the stream, state re-read on change; dev-only proven by the build (Phase 4, Part B)

**Decision:**

* **Screen data.** The run screen has two data sources with one job each. The
  SSE stream (read with the `fetch()` client of D19/D23) provides the event
  log. The stage timeline and status come from `GET /api/runs/{id}`, re-read
  whenever a `stage.*`, `pipeline.*` or `api.run_*` event arrives and once
  more when the stream closes. Only the newest response is applied, so two
  overlapping reads can never put an older state back on screen.
* **New-run rules.** They live in a plain module, `src/runRequest.js`, so they
  are unit-tested without a DOM. `freshForm()` returns a new object with only
  the execution gate ticked, and the form resets to it after every submit.
* **Mock model.** The development-only menu (D27 rule 2) is its own module,
  rendered only under `import.meta.env.DEV`, so a production build drops it
  along with the mock model's name. A test builds both bundles. The
  production one must not contain the mock model's name or the menu's text,
  and the development one must contain both, so the check can't pass
  vacuously.
* **Smoke build.** `npm run smoke` builds in development mode, because the
  offline mock model it drives is only reachable from that menu. `npm start`
  always rebuilds for production.
* **Stage selection.** Not in the form; runs use the configured stages.

**Why:**
* Events are append-only and cheap, and the run state is already assembled
  by the API. Re-reading it on the few events that change it is simpler, and
  cannot drift, compared with rebuilding the board from events in the UI.
* "Mock only in development" is a promise about what ships, so it is checked
  on the shipped artifact rather than on the source.

**Cost accepted:**
* One extra GET per state-changing event, a few dozen per run on loopback.
* The smoke test exercises a development bundle. The production bundle's
  security is the same, because the CSP and bridge come from the main
  process, and its content is checked by the build test.

---

## D29 — Approval dialogs: inert model text, verbatim command, no accidental approve (Phase 4, Part B)

**Decision:** a run waiting at a gate opens a modal `<dialog>`
(`showModal()`, so the page behind it is inert).

* **Plan and architecture gates.** The dialog shows the stage's full output
  from the run state (the gate payload is cut at 4,000 characters), rendered
  by `SafeMarkdown`:
  * `react-markdown` 10 builds React elements and never sets innerHTML;
  * `skipHtml` drops raw HTML;
  * an allowlist of plain text elements (headings, paragraphs, lists,
    emphasis, code, quotes); anything else is unwrapped to its text;
  * links and images are replaced with plain text showing their target
    exactly as written, so nothing is clickable or fetched, and a
    `javascript:` or look-alike URL is visible for what it is.
* **Execution gate.** The command, working folder and time limit are shown
  verbatim in `<pre>`, never through markdown. Its button reads
  **Approve and run**.
* **Focus.** No button has initial focus. The dialog focuses the note field,
  so Enter cannot approve.
* **Deciding later.** Escape or **Decide later** closes the dialog without
  deciding; the run keeps waiting and **Review and decide** reopens it. Each
  new wait (a gate can reopen, e.g. the execution gate on a fix-loop rerun)
  opens a fresh dialog with an empty note.
* **Reject.** A rejected run ends `failed` with the note as its error. A
  `409` (the run was cancelled, or is no longer waiting) is shown as "no
  longer waiting" rather than as an error.
* **Tests:**
  * `test/markdown.test.js` renders 15 hostile inputs (script, `img onerror`,
    raw anchors, iframe, style, `svg onload`, `javascript:`, https,
    autolinks, bare URLs, reference links, markdown and `data:` images,
    comments, entities). It asserts only allowlisted tags and our own
    `class` attribute appear. It was checked against mutations: removing the
    link replacement fails 5 tests, and turning off `skipHtml` fails 4.
  * The smoke test drives all three dialogs in real Electron: approve plan
    and architecture, then reject the execution gate on a detected
    `python -m pytest -q`. The dialog must be inert, show the exact
    command, folder and 300 s limit, and nothing may run.

**Why:** the approval dialog is where model output meets the button that runs
code on the user's machine. Model text must be unable to become a link, an
image, a script or a fake button, and the command must be shown exactly as it
will run. D26's CSP is the second line: even a renderer bug could not run
script or send data anywhere but the API.

**Cost accepted:** no tables (GitHub-flavoured markdown is not enabled) and no
clickable links in plans. Both can be added later as deliberately reviewed
changes. The dialog is modal, so the event log behind it can't be scrolled
until you choose **Decide later**.

---

## D30 — Run files: read-only, one run's folder, shown as text (Phase 4, Part B)

**Decision:** two new token-guarded routes, both read-only (other methods are
`405`), give the UI the files of one run.

* **Listing.** `GET /api/runs/{id}/files` lists what is actually on disk in
  that run's project folder, not what the manifest claims.
  * Sorted relative paths and sizes, capped at 500.
  * `.git`, `node_modules`, `.venv`/`venv`, `__pycache__` and tool caches
    are skipped.
  * Directory links are not followed, and a file whose resolved path leaves
    the folder is not listed.
* **Reading.** `GET /api/runs/{id}/files/content?path=` returns one file.
  * The path goes through `safe_relative_path` (D15), is resolved, and must
    still be inside the folder, so `..`, absolute paths, drive letters, UNC
    paths, NUL bytes, device names, symlinks and junctions pointing outside
    are all a `400`.
  * At most 256 KiB is returned, with `truncated`, and a cut never splits a
    UTF-8 character.
  * A file with a NUL byte, or one that isn't valid UTF-8, comes back as
    `binary: true` with no content.
* **Which folder.** The run records its project folder when its pipeline is
  built, like `board_path`, because a runtime may be rooted elsewhere than the
  API. A run that hasn't started has an empty listing, and reading from it is
  a `404`.
* **UI.**
  * The Files tab shows contents only as text in a `<pre>`, never as markdown
    or HTML, whatever the extension.
  * The Task board tab renders stage output through `SafeMarkdown` (D29).
  * The routes join the pinned API surface (D24), which is now 15 routes.
* **Tests.**
  * pytest covers real manifest files, nine escape paths, a Windows junction
    pointing outside (junctions need no special rights, so they are the real
    risk there), a symlink (skipped without symlink rights), the cap with a
    split character, binary and non-UTF-8 files, skipped folders, the listing
    cap, a queued run, and read-only methods.
  * The smoke test opens a hostile `notes.md` (script, `img onerror`,
    `javascript:` link) in the viewer and checks it shows as inert text and
    changes nothing.

**Why:** reviewing a run means reading what it produced, and D15's sandbox
already defines exactly which folder that is. This deliberately relaxes
`/api/projects`' "never file contents" rule, but only per run, read-only,
bounded, and behind the token. Showing contents as text is what keeps a
model-written file from becoming markup in the window that holds the approve
button.

**Also fixed in B4:** the run screen swallowed a failed
`GET /api/runs/{id}`, which could leave a stale state on screen, for example
no approval dialog for a run that is waiting, until the next event. A
failed read is now retried every second while it is still the newest. The
smoke failed once in 19 hidden-window runs with exactly that symptom. The
retry is still right, but it was not the cause. The real cause was found in
B5: a queued dialog `close` event, see D31. The smoke now records the page
state on any failure.

**Cost accepted:** no syntax highlighting, no editing, and no view of files
outside the run's own folder. Large files show only their first 256 KiB.

---

## D31 — Settings screen; the settings API reports ignored overrides; dialog close race fixed (Phase 4, Part B)

**Decision:**

* **Settings screen.** A Settings screen next to Runs has three parts.
  * *Models.* A first-choice model per agent (the `AgentPin` of D22), with
    the full chain the next run will use shown beside it.
  * *Provider order.* Up and down buttons, not drag and drop. The order is
    sent only if it was changed here, so opening and saving the screen never
    rewrites a setting you did not touch. An existing pin that is not in
    the list (e.g. the mock, pinned in development) stays selectable for the
    same reason.
  * *API keys.* Write-only password fields. Keys are sent once, the fields
    are cleared, and only `set` or `missing` is shown back.
  * The offline mock providers (`kind: "mock"`) are left out of the model
    lists outside development (D27).
* **Rules module.** The rules live in `src/settingsModel.js` and are
  unit-tested (7 tests).
* **API additions.**
  * `SettingsResponse` gains `warnings` and each provider's `kind`.
  * `Registry.load_effective` now also returns why saved overrides were
    ignored. Its two callers were updated.
  * The Settings tab shows a red **!** from startup while there is a
    warning, and the screen names the file and the reason. Runs keep
    working on the defaults meanwhile, as D22 intended, and the smoke test
    proves it by running with a corrupt file present from the start.
* **Save feedback.** The save message is kept outside the component that
  remounts from each saved result; before, the "Saved" message vanished
  immediately. Each section counts finished saves (`data-saves`), so a new
  result can be told apart from the previous one.
* **Approval-dialog race.** The race behind the intermittent smoke failure
  of B4 is fixed. A `<dialog>`'s `close` event is queued, not synchronous.
  After **Decide later** followed quickly by **Review and decide**, the first
  close's event arrived after the reopen, marked the wait as dismissed, and
  closed the reopened dialog, leaving a waiting run with no dialog. The
  handler now counts a `close` event only if the dialog is still closed
  when it arrives.
  * *How it was found.* The smoke's failure snapshot showed the dialog
    present but closed. A temporary trace of every open, close and dismiss
    showed the stale event landing after the reopen.
  * *Result.* 12 of 12 smoke runs passed after the fix; the last batch
    before it failed 2 of 3.

**Why:** settings are where a user fixes a broken configuration, so the
screen must work when the saved file is broken, say so plainly, and never
change anything the user did not touch. The dialog race is exactly the kind
of bug that makes a waiting run look stuck. It was found because the smoke
records the page state when it fails, instead of just being retried.

**Cost accepted:** no drag-and-drop ordering, no per-key delete (D22), and
the provider order list includes every non-mock provider, even ones no
agent uses.

---

## Phase plan

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Research, provider reality check, architecture decisions | done |
| 1 | Repo scaffold, config layer, provider layer (router, retries, quota, budgets, events), CLI, tests | done |
| 2 | Specialist agents (planner, architect, coder, tester, reviewer, devops, docs) + orchestrator with shared task board | done |
| 3 | Workspace execution: generate files, run tests/builds, iterate | done |
| 4 | FastAPI backend + Electron/React desktop UI | in progress (Part A, the API, done; Part B: B1–B5 done, B6 next) |
| 5 | Deploy generated apps (Vercel + Render) | not started |

