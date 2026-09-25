// node --test: the shell's backend launch (D25) against the real Python API,
// started exactly as Electron starts it, in a throwaway root. Offline.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";

import shell from "../electron/backend.cjs";

const { parseReady, newToken, startBackend, stopBackend, REPO_ROOT } = shell;

test("SDA_READY is the only thing parsed, and the port must be sane", () => {
  assert.equal(parseReady(""), null);
  assert.equal(parseReady("some other line\n"), null);
  assert.equal(parseReady('SDA_READY {"port": 51234}\n'), 51234);
  assert.equal(parseReady('SDA_READY {"port": 51234}\r\n'), 51234);
  assert.throws(() => parseReady('SDA_READY {"port": 0}\n'), /invalid port/);
  assert.throws(() => parseReady('SDA_READY {"port": "80"}\n'), /invalid port/);
});

test("tokens are 256-bit, URL-safe and fresh each time", () => {
  const a = newToken();
  assert.match(a, /^[A-Za-z0-9_-]{43}$/);
  assert.notEqual(a, newToken());
});

let root;
let backend;

before(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "sda-shell-"));
  fs.cpSync(path.join(REPO_ROOT, "config"), path.join(root, "config"), { recursive: true });
  backend = await startBackend({ root });
});

after(async () => {
  await stopBackend(backend?.child);
  fs.rmSync(root, { recursive: true, force: true });
});

test("the backend starts with the shell's token and answers health", async () => {
  assert.match(backend.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/);
  const ok = await fetch(`${backend.baseUrl}/api/health`, { headers: { "X-API-Key": backend.token } });
  assert.equal(ok.status, 200);
  assert.equal(`http://127.0.0.1:${(await ok.json()).api_port}`, backend.baseUrl);
  const refused = await fetch(`${backend.baseUrl}/api/health`, { headers: { "X-API-Key": newToken() } });
  assert.equal(refused.status, 401);
  // the token never shows up in anything the backend wrote
  assert.ok(!backend.stderrTail().includes(backend.token));
});

test("the page's real origin is allowed (CORS preflight from sda://app)", async () => {
  const preflight = await fetch(`${backend.baseUrl}/api/health`, {
    method: "OPTIONS",
    headers: {
      Origin: "sda://app",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": "X-API-Key",
    },
  });
  assert.equal(preflight.headers.get("access-control-allow-origin"), "sda://app");
});

test("closing stdin shuts the backend down gracefully", async () => {
  await stopBackend(backend.child);
  assert.equal(backend.child.exitCode, 0, "exited on its own, not killed");
});

test("a backend that dies before SDA_READY is reported, not waited on", async () => {
  // node is not python: it exits at once on "-m backend.api"
  process.env.SDA_PYTHON = process.execPath;
  try {
    const started = Date.now();
    await assert.rejects(startBackend({ timeoutMs: 20000 }), /exited before it was ready \(exit code \d+\)/);
    assert.ok(Date.now() - started < 5000, "failed fast, did not sit out the timeout");
  } finally {
    delete process.env.SDA_PYTHON;
  }
});

test("an interpreter that cannot be started is reported", async () => {
  process.env.SDA_PYTHON = path.join(root, "no-such-python.exe");
  try {
    await assert.rejects(startBackend(), /could not start/);
  } finally {
    delete process.env.SDA_PYTHON;
  }
});
