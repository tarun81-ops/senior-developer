// node --test: the new-run rules (D27) and what a production build contains.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "vite";

import { buildRunRequest, freshForm } from "../src/runRequest.js";

test("a new form has the execution gate on, and only that one", () => {
  assert.deepEqual(freshForm().gates, { plan: false, architecture: false, execution: true });
});

test("each form is a fresh copy, so an unticked gate is never remembered", () => {
  const first = freshForm();
  first.gates.execution = false;
  assert.equal(freshForm().gates.execution, true);
});

test("the request body carries the gates that are ticked, in pipeline order", () => {
  const form = freshForm();
  form.request = "  a tiny calculator  ";
  form.gates.plan = true;
  assert.deepEqual(buildRunRequest(form), {
    request: "a tiny calculator",
    approval_gates: ["plan", "execution"],
  });
  form.gates = { plan: false, architecture: false, execution: false };
  form.project = " calc ";
  assert.deepEqual(buildRunRequest(form), { request: "a tiny calculator", approval_gates: [], project: "calc" });
});

test("no model is chosen unless an override is passed, and an empty request is refused", () => {
  const form = { ...freshForm(), request: "x" };
  assert.ok(!("provider" in buildRunRequest(form)) && !("model" in buildRunRequest(form)));
  assert.deepEqual(
    buildRunRequest(form, { provider: "mock", model: "mock-echo" }),
    { request: "x", approval_gates: ["execution"], provider: "mock", model: "mock-echo" },
  );
  assert.throws(() => buildRunRequest({ ...freshForm(), request: "   " }), /Describe/);
});

async function bundleText(mode) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), `sda-build-${mode}-`));
  const previous = process.env.NODE_ENV;
  // Vite reads NODE_ENV once per process; set it per build so modes cannot leak
  process.env.NODE_ENV = mode;
  try {
    await build({
      configFile: fileURLToPath(new URL("../vite.config.js", import.meta.url)),
      mode,
      logLevel: "silent",
      build: { outDir, emptyOutDir: true },
    });
    const assets = path.join(outDir, "assets");
    return fs.readdirSync(assets).filter((f) => f.endsWith(".js"))
      .map((f) => fs.readFileSync(path.join(assets, f), "utf8")).join("\n");
  } finally {
    if (previous === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previous;
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

test("a production build has no developer menu and cannot name the mock model", async () => {
  const production = await bundleText("production");
  assert.ok(!production.includes("mock-echo"), "mock model name is in the production bundle");
  assert.ok(!production.includes("offline mock model"), "developer menu is in the production bundle");
  // and the check is real: a development build does contain both
  const development = await bundleText("development");
  assert.ok(development.includes("mock-echo") && development.includes("offline mock model"));
});
