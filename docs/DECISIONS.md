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

---

## Phase plan

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Research, provider reality check, architecture decisions | done |
| 1 | Repo scaffold, config layer, provider layer (router, retries, quota, budgets, events), CLI, tests | done |
| 2 | Specialist agents (planner, architect, coder, tester, reviewer, devops, docs) + orchestrator with shared task board | approved, not started |
| 3 | Workspace execution: generate files, run tests/builds, iterate | not started |
| 4 | FastAPI + React/Vite + Electron desktop UI streaming `events.jsonl` | not started |
| 5 | Deploy generated apps (Vercel + Render) | not started |

