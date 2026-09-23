# senior-developer-agents

A multi-agent software engineering system: you describe an app in plain English,
an orchestrator routes the work to specialist agents (planner, architect,
coder, tester, reviewer, devops, docs), and they build it for you.

It runs **exclusively on free-tier LLM APIs** (Gemini, Groq, OpenRouter) and is
built so that hitting a free-tier limit is a normal, handled event — not a
crash.

> **Current state: Phase 1 (foundation) complete.**
> The provider layer (routing, retries, failover, quota tracking, budgets,
> event log) is implemented, tested and runnable from the command line.
> The specialist agents arrive in Phase 2, the desktop UI in Phase 4.

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
| `demo` | Offline demonstration: first provider always answers 429, second succeeds — watch retry → backoff → cooldown → failover → success. |
| `events -n 25` | Tails the JSONL event log of the most recent run (`--kind`, `--run-id` to filter). |

Exit codes: `0` ok · `1` configuration/setup problem · `2` runtime failure
(all providers failed) · `3` run budget exhausted (needs a human).

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

## Project layout

```
config/                 providers.yaml, agents.yaml, limits.yaml (all config lives here)
backend/
  cli.py                doctor / providers / models / ask / demo / events
  core/
    config.py           Settings + .env loading
    errors.py           error hierarchy (retryable vs not)
    secrets.py          env + Windows Credential Manager key lookup
    runtime.py          composition root (the one place that wires everything)
    provider/           schemas, registry, quota ledger, HTTP client, router
    agents/             Agent base class, JSON extractor, prompts/*.md
    events/             EventBus + JSONL writer
    orchestrator/       BudgetTracker (Phase 2 adds the orchestrator here)
  tests/                77 tests, no network, no keys required
docs/DECISIONS.md       why each Phase 0 decision was made
scripts/setup.ps1       one-shot Windows setup
data/                   runtime state (quota, runs, events) — git-ignored
workspace/              where generated apps will live — git-ignored
```

---

## Tests

```powershell
.\.venv\Scripts\python -m pytest          # 77 tests, ~3s, offline
```

The suite covers the quota ledger, backoff maths, router failover order, HTTP
error classification (via a fake transport), agent JSON extraction, the event
log, and the CLI end-to-end against a throwaway project root. No test touches
the network or needs an API key.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 User not found.` from OpenRouter | Key revoked/stale — create a new one at openrouter.ai/keys. (Listing models works anyway: `/models` is public and does **not** validate your key.) |
| Provider silently missing from the chain | It is on cooldown or has no key. `python -m backend.cli providers` shows both; `--clear-cooldowns` after fixing a key. |
| `No endpoints found for <model>` / 404 | Model id retired. Re-run `models --live` and update the YAML. |
| Gemini daily quota exhausted | Free tier resets at **midnight Pacific**. The ledger knows this and shows the reset boundary in `providers`. |
| `BUDGET STOP` (exit 3) | A run exceeded its call/token budget. Intentional — review `data/runs/<run_id>/budget.json`, then raise the limit in `config/limits.yaml` if it was correct. |
| Installing on Python 3.14 fails | Use 3.12 or 3.13: `py -3.12 -m venv .venv`. |

**Never** paste real keys into prompts, and note that Google may use free-tier
Gemini prompts to improve its models — no passwords, private keys or customer
data in prompts.

---

## Roadmap

- **Phase 1 (done)** — provider layer: config-driven routing, retries,
  failover, quota ledger, budgets, event log, CLI, 77 tests.
- **Phase 2** — specialist agents + orchestrator: planner, architect, coder,
  tester, reviewer, devops, docs; shared task board and hand-off protocol.
- **Phase 3** — workspace execution: generate files, run `npm`/`pytest`,
  iterate on failures, everything confined to `workspace/`.
- **Phase 4** — FastAPI backend + React/Vite + Electron desktop UI replaying
  `events.jsonl` live.
- **Phase 5** — deploy: Vercel (frontend) + Render (backend).


