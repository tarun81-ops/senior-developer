// Starts and stops the Python API for the shell (D25).
//
// The shell generates the per-launch token and hands it to the backend as the
// first line of the child's stdin, a pipe private to the two processes. The
// token is never in argv, the environment, a file or any output. The one line
// read from the backend's stdout is `SDA_READY {"port": N}`; after it, stdout
// is drained unread. stdin stays open as the backend's lifeline: closing it
// shuts the API down gracefully, and it closes by itself if this process dies.

const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const READY_LINE = /^SDA_READY (\{.*\})\r?$/m;
const MAX_HANDSHAKE_BYTES = 64 * 1024;
const STDERR_TAIL_BYTES = 4096;

/** The port from an `SDA_READY` line, or `null` if none has arrived (yet). */
function parseReady(stdout) {
  const match = READY_LINE.exec(stdout);
  if (!match) return null;
  const { port } = JSON.parse(match[1]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`backend announced an invalid port: ${match[1]}`);
  }
  return port;
}

/** A fresh 256-bit URL-safe token (43 chars), matching the backend's check. */
function newToken() {
  return crypto.randomBytes(32).toString("base64url");
}

/**
 * The backend's environment: this process's, minus anything that would let
 * another Python installation leak in. User site-packages is off
 * (PYTHONNOUSERSITE), so the bundled interpreter never borrows packages the
 * user happens to have. Generated projects' commands inherit this too (the
 * runner copies os.environ). See D34.
 */
function isolatedEnv() {
  const env = { ...process.env, PYTHONUNBUFFERED: "1", PYTHONNOUSERSITE: "1" };
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  return env;
}

/** The venv interpreter, or SDA_PYTHON when set. */
function pythonPath() {
  if (process.env.SDA_PYTHON) return process.env.SDA_PYTHON;
  const venv = process.platform === "win32"
    ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(REPO_ROOT, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python";
}

/**
 * Spawn the backend and resolve `{ baseUrl, token, child, stderrTail }` once
 * it is listening. Rejects, with the exit code and the end of stderr, if the
 * process cannot start, exits, or stays silent past `timeoutMs`; it never
 * waits forever for SDA_READY.
 *
 * `python` and `cwd` default to the repo's .venv and checkout; the installed
 * app passes its bundled interpreter and backend folder (D34).
 */
function startBackend({ root, timeoutMs = 30000, python = pythonPath(), cwd = REPO_ROOT } = {}) {
  const token = newToken();
  const args = ["-m", "backend.api", "--port", "0", "--token-stdin", "--exit-with-stdin"];
  if (root) args.push("--root", root);
  const child = spawn(python, args, {
    cwd,
    env: isolatedEnv(),
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });

  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr = (stderr + chunk).slice(-STDERR_TAIL_BYTES); });
  const stderrTail = () => stderr.trim();
  child.stdin.on("error", () => {}); // a child that died early closes the pipe
  child.stdin.write(`${token}\n`);

  return new Promise((resolve, reject) => {
    let stdout = "";
    let settled = false;
    const finish = (err, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.stdout.off("data", onData);
      child.off("exit", onExit);
      child.off("error", onError);
      child.stdout.resume(); // keep draining, unread, so the pipe never fills
      if (err) {
        child.kill();
        reject(new Error(`${err}${stderrTail() ? `\n\n${stderrTail()}` : ""}`));
      } else resolve(value);
    };
    const onExit = (code) => finish(`backend exited before it was ready (exit code ${code})`);
    const onError = (err) => finish(`could not start ${python}: ${err.message}`);
    const onData = (chunk) => {
      stdout += chunk;
      try {
        const port = parseReady(stdout);
        if (port) finish(null, { baseUrl: `http://127.0.0.1:${port}`, token, child, stderrTail });
        else if (stdout.length > MAX_HANDSHAKE_BYTES) finish("backend never sent SDA_READY");
      } catch (err) {
        finish(err.message);
      }
    };
    const timer = setTimeout(() => finish(`backend did not start within ${timeoutMs / 1000}s`), timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", onData);
    child.once("exit", onExit);
    child.once("error", onError);
  });
}

/** Graceful stop: close the lifeline; hard-kill only if it hangs. */
function stopBackend(child, graceMs = 8000) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => { child.kill(); resolve(); }, graceMs);
    child.once("exit", () => { clearTimeout(timer); resolve(); });
    child.stdin.end();
  });
}

module.exports = { parseReady, newToken, startBackend, stopBackend, REPO_ROOT };
