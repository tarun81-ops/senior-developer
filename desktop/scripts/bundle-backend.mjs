// npm run bundle: build what the installer ships next to Electron (D34).
//
//   build/python/        the official CPython for Windows (python.org's NuGet
//                        package), with requirements-app.txt installed into it
//   build/backend-root/  backend/ (without tests) + config/
//
// The package is pinned by version, size and SHA-512 and cached in
// build/cache/. Dependencies are constrained to the exact versions installed
// in the repo's .venv, so the bundle is what the test suite ran against.
// The bundle is verified before this script reports success.
import { execFileSync, spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PYTHON = {
  version: "3.14.7",
  size: 15623417,
  sha512: "MX3tQ1/8rOfHRN/xsJPEkESz7ALdq1dhR16E+bmFrqKCkXF+09QsiwHW3ZOq9uqTQ0STwYgt92UIU9E6mMKTgg==",
};
const url = `https://api.nuget.org/v3-flatcontainer/python/${PYTHON.version}/python.${PYTHON.version}.nupkg`;

const DESKTOP = path.resolve(fileURLToPath(import.meta.url), "..", "..");
const REPO = path.resolve(DESKTOP, "..");
const BUILD = path.join(DESKTOP, "build");
const CACHE = path.join(BUILD, "cache");
const PY_DIR = path.join(BUILD, "python");
const PY_EXE = path.join(PY_DIR, "python.exe");
const BACKEND_ROOT = path.join(BUILD, "backend-root");

const log = (msg) => console.log(`bundle: ${msg}`);

/**
 * The bundled interpreter must see only its own site-packages. Without this,
 * pip treats packages in the user's %APPDATA%\Python site-packages as already
 * installed and skips them, and the app then works only on machines that
 * happen to have them. The installed app launches it the same way
 * (electron/backend.cjs).
 */
const ISOLATED_ENV = { ...process.env, PYTHONNOUSERSITE: "1" };
delete ISOLATED_ENV.PYTHONPATH;
delete ISOLATED_ENV.PYTHONHOME;

const run = (exe, args, opts = {}) => execFileSync(exe, args, { stdio: "inherit", env: ISOLATED_ENV, ...opts });

async function fetchPython() {
  fs.mkdirSync(CACHE, { recursive: true });
  const file = path.join(CACHE, `python.${PYTHON.version}.nupkg`);
  if (!fs.existsSync(file)) {
    log(`downloading ${url}`);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`download failed: HTTP ${response.status}`);
    fs.writeFileSync(`${file}.part`, Buffer.from(await response.arrayBuffer()));
    fs.renameSync(`${file}.part`, file);
  }
  const data = fs.readFileSync(file);
  const hash = crypto.createHash("sha512").update(data).digest("base64");
  if (data.length !== PYTHON.size || hash !== PYTHON.sha512) {
    fs.rmSync(file);
    throw new Error("python package failed its size/SHA-512 check; the cached copy was deleted");
  }
  log(`python ${PYTHON.version} verified (SHA-512)`);
  return file;
}

function extractPython(nupkg) {
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "sda-python-"));
  try {
    // Windows' own tar.exe (bsdtar) reads zip files; named explicitly because
    // Git Bash's GNU tar on PATH does not. The interpreter lives in tools/.
    const tar = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
    run(tar, ["-xf", nupkg, "-C", staging, "tools"]);
    fs.rmSync(PY_DIR, { recursive: true, force: true });
    fs.cpSync(path.join(staging, "tools"), PY_DIR, { recursive: true });
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

function installDependencies() {
  const venvPython = path.join(REPO, ".venv", "Scripts", "python.exe");
  const constraints = path.join(BUILD, "constraints.txt");
  // exactly the versions the test suite ran against
  fs.writeFileSync(constraints, execFileSync(venvPython, ["-m", "pip", "freeze", "--all"], { encoding: "utf8" }));
  run(PY_EXE, ["-m", "ensurepip", "--default-pip"], { stdio: "ignore" });
  run(PY_EXE, [
    "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check", "-q",
    "-r", path.join(REPO, "requirements-app.txt"), "-c", constraints,
  ]);
}

function copyBackend() {
  fs.rmSync(BACKEND_ROOT, { recursive: true, force: true });
  const skip = (src) => {
    const name = path.basename(src);
    return !(name === "__pycache__" || name === "tests" || name.endsWith(".pyc"));
  };
  fs.cpSync(path.join(REPO, "backend"), path.join(BACKEND_ROOT, "backend"), { recursive: true, filter: skip });
  fs.cpSync(path.join(REPO, "config"), path.join(BACKEND_ROOT, "config"), { recursive: true });
}

/** The bundle must run the backend and a generated project's tests. */
async function verify() {
  // The dev-only fake deploy targets live in backend/tests (D41): they must not ship.
  if (fs.existsSync(path.join(BACKEND_ROOT, "backend", "tests"))) {
    throw new Error("backend/tests is in the bundle; the dev-only fake deploy targets must not ship");
  }
  // every module must come from the bundle itself, never from a user site
  run(PY_EXE, ["-c", [
    "import sys, site, fastapi, uvicorn, httpx, pydantic, yaml, dotenv, tzdata, pytest",
    "assert not site.ENABLE_USER_SITE, 'user site-packages is enabled'",
    "mods = (fastapi, uvicorn, httpx, pydantic, yaml, dotenv, tzdata, pytest)",
    "bad = [m.__name__ for m in mods if not m.__file__.startswith(sys.prefix)]",
    "assert not bad, f'imported from outside the bundle: {bad}'",
  ].join("\n")]);

  // a real launch, exactly as the installed app does it, on an empty data root
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "sda-bundle-root-"));
  const token = crypto.randomBytes(32).toString("base64url");
  const child = spawn(PY_EXE, ["-m", "backend.api", "--port", "0", "--root", dataRoot, "--token-stdin", "--exit-with-stdin"], {
    cwd: BACKEND_ROOT, stdio: ["pipe", "pipe", "inherit"], windowsHide: true, env: ISOLATED_ENV,
  });
  child.stdin.write(`${token}\n`);
  try {
    const port = await new Promise((resolve, reject) => {
      let out = "";
      const timer = setTimeout(() => reject(new Error("bundled backend did not start")), 30000);
      child.on("exit", (code) => reject(new Error(`bundled backend exited (${code})`)));
      child.stdout.on("data", (chunk) => {
        out += chunk;
        const m = /^SDA_READY (\{.*\})/m.exec(out);
        if (m) { clearTimeout(timer); resolve(JSON.parse(m[1]).port); }
      });
    });
    const headers = { "X-API-Key": token };
    const health = await fetch(`http://127.0.0.1:${port}/api/health`, { headers });
    const settings = await fetch(`http://127.0.0.1:${port}/api/settings`, { headers });
    if (health.status !== 200 || settings.status !== 200) {
      throw new Error(`bundled backend answered health ${health.status}, settings ${settings.status}`);
    }
    log("bundled backend starts, answers health and settings from an empty data root");
  } finally {
    child.removeAllListeners("exit");
    child.stdin.end();
    await new Promise((r) => child.once("exit", r));
    fs.rmSync(dataRoot, { recursive: true, force: true });
  }

  // what the runner does for a generated project: `python` is this interpreter
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "sda-bundle-project-"));
  try {
    fs.mkdirSync(path.join(project, "tests"));
    fs.writeFileSync(path.join(project, "calc.py"), "def add(a, b):\n    return a + b\n");
    fs.writeFileSync(path.join(project, "tests", "test_calc.py"), "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n");
    run(PY_EXE, ["-m", "pytest", "-q", "-p", "no:cacheprovider"], { cwd: project, stdio: "ignore" });
    log("generated-project tests run with the bundled python -m pytest");
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
}

extractPython(await fetchPython());
log("installing dependencies (constrained to the .venv versions)");
installDependencies();
copyBackend();
await verify();
log(`done: ${path.relative(DESKTOP, PY_DIR)}, ${path.relative(DESKTOP, BACKEND_ROOT)}`);
