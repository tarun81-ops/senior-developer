// node --test: settings screen rules (D27, D31).
import assert from "node:assert/strict";
import { test } from "node:test";

import { draftFrom, keysToSend, modelOptions, move, pickableProviders, updateFrom } from "../src/settingsModel.js";

const PROVIDERS = [
  { provider: "gemini", label: "Gemini", kind: "openai_compatible", models: ["g-flash", "g-pro"] },
  { provider: "groq", label: "Groq", kind: "openai_compatible", models: ["llama"] },
  { provider: "mock", label: "Offline mock", kind: "mock", models: ["mock-echo"] },
  { provider: "mock_flaky", label: "Flaky mock", kind: "mock", models: ["flaky"] },
];
const SETTINGS = {
  agents: [
    { agent: "coder", routing: [], override: { provider: "groq", model: "llama" } },
    { agent: "planner", routing: [], override: null },
  ],
  providers: PROVIDERS,
  provider_order: [],
};

test("mock providers are not offered outside development", () => {
  assert.deepEqual(pickableProviders(PROVIDERS).map((p) => p.provider), ["gemini", "groq"]);
  assert.equal(pickableProviders(PROVIDERS, { dev: true }).length, 4);
  const values = modelOptions(PROVIDERS, null).map((o) => o.value);
  assert.deepEqual(values, ["gemini/g-flash", "gemini/g-pro", "groq/llama"]);
});

test("an existing pin outside the list stays selectable, so saving never drops it", () => {
  const options = modelOptions(PROVIDERS, { provider: "mock", model: "mock-echo" });
  assert.equal(options.at(-1).value, "mock/mock-echo");
  assert.match(options.at(-1).label, /not normally offered/);
});

test("opening and saving without changes sends back exactly what was saved", () => {
  const draft = draftFrom(SETTINGS);
  assert.deepEqual(draft.order, ["gemini", "groq"]);
  assert.deepEqual(updateFrom(draft, SETTINGS), {
    agents: { coder: { provider: "groq", model: "llama" } },
    provider_order: [],
  });
});

test("a changed pin and a changed order are both sent", () => {
  const draft = draftFrom(SETTINGS);
  draft.pins.planner = { provider: "gemini", model: "g-pro" };
  draft.pins.coder = null; // back to the configured chain
  draft.order = move(draft.order, 1, -1);
  draft.orderTouched = true;
  assert.deepEqual(updateFrom(draft, SETTINGS), {
    agents: { planner: { provider: "gemini", model: "g-pro" } },
    provider_order: ["groq", "gemini"],
  });
});

test("a saved order comes first, other providers follow in config order", () => {
  assert.deepEqual(draftFrom({ ...SETTINGS, provider_order: ["groq"] }).order, ["groq", "gemini"]);
});

test("move stays in bounds", () => {
  assert.deepEqual(move(["a", "b"], 0, -1), ["a", "b"]);
  assert.deepEqual(move(["a", "b"], 1, 1), ["a", "b"]);
  assert.deepEqual(move(["a", "b", "c"], 2, -1), ["a", "c", "b"]);
});

test("only typed keys are sent, trimmed", () => {
  assert.deepEqual(keysToSend({ GEMINI_API_KEY: "  AIza-x  ", GROQ_API_KEY: "", OPENROUTER_API_KEY: "   " }), {
    GEMINI_API_KEY: "AIza-x",
  });
});

test("deploy keys are grouped apart from model provider keys (D41)", async () => {
  const { keyGroups } = await import("../src/settingsModel.js");
  const names = ["GEMINI_API_KEY", "GITHUB_TOKEN", "GROQ_API_KEY", "RENDER_API_KEY", "RENDER_OWNER_ID"];
  assert.deepEqual(keyGroups(names), [
    ["GEMINI_API_KEY", "GROQ_API_KEY"],
    ["GITHUB_TOKEN", "RENDER_API_KEY", "RENDER_OWNER_ID"],
  ]);
});
