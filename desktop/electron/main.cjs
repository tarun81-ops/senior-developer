// Electron main process (D25, D26): starts the backend, serves the UI from
// sda://app, and keeps the one window locked down.

const { app, BrowserWindow, Menu, dialog, ipcMain, net, protocol, session } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { startBackend, stopBackend } = require("./backend.cjs");

// Must match APP_ORIGIN in backend/api/security.py exactly.
const SCHEME = "sda";
const APP_ORIGIN = `${SCHEME}://app`;
const DIST = path.join(__dirname, "..", "dist");
const DEV_URL = process.env.SDA_DEV_URL; // set only by scripts/dev.mjs
const SMOKE = process.argv.includes("--smoke");

/**
 * Where the backend comes from (D34). Installed: the bundled Python and
 * backend in resources/, and user data under
 * %APPDATA%\Senior Developer Agents (config and prompts stay in the install
 * folder, D33).
 * Development: the repo's .venv and checkout.
 */
function backendLaunch() {
  if (!app.isPackaged) return {};
  return {
    python: path.join(process.resourcesPath, "python", "python.exe"),
    cwd: path.join(process.resourcesPath, "backend-root"),
  };
}

function dataRoot() {
  if (process.env.SDA_ROOT) return process.env.SDA_ROOT;
  if (SMOKE) return smokeRoot();
  return app.isPackaged ? app.getPath("userData") : undefined; // undefined: the repo
}

// Before `ready`. A standard + secure scheme gives the page a real origin,
// "sda://app", instead of the "null" a file:// page sends. The page can then
// use fetch with CORS against the API.
protocol.registerSchemesAsPrivileged([
  {
    scheme: SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
  },
]);

let backend = null; // { baseUrl, token, child, stderrTail }
let quitting = false;

/** The page may load only its own files and talk only to the API. */
function csp() {
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src ${backend.baseUrl}`,
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
  ].join("; ");
}

/** Serve desktop/dist at sda://app/..., nothing outside it, always with the CSP. */
function serveDist() {
  protocol.handle(SCHEME, async (request) => {
    const url = new URL(request.url);
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
    const file = path.resolve(DIST, relative);
    if (url.host !== "app" || !file.startsWith(DIST + path.sep) || !fs.existsSync(file)) {
      return new Response("Not found", { status: 404 });
    }
    const response = await net.fetch(pathToFileURL(file).toString());
    const headers = new Headers(response.headers);
    headers.set("Content-Security-Policy", csp());
    headers.set("X-Content-Type-Options", "nosniff");
    return new Response(response.body, { status: response.status, headers });
  });
}

function isOurPage(url) {
  if (!url) return false;
  if (DEV_URL) return url === DEV_URL || url.startsWith(`${DEV_URL}/`);
  return url.startsWith(`${APP_ORIGIN}/`);
}

function lockDownSession() {
  // No camera, microphone, notifications, clipboard reads, etc.
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "Senior Developer Agents",
    show: !SMOKE || Boolean(process.env.SDA_SMOKE_SCREENSHOT), // hidden windows stop painting
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      devTools: Boolean(DEV_URL),
    },
  });
  // The window only ever shows our UI: no navigation away, no pop-ups, no webviews.
  win.webContents.on("will-navigate", (event, url) => {
    if (!isOurPage(url)) event.preventDefault();
  });
  win.webContents.on("will-attach-webview", (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.loadURL(DEV_URL || `${APP_ORIGIN}/index.html`);
  return win;
}

// The bridge's one call. Only our own top-level page gets an answer, and the
// answer is exactly these two values.
ipcMain.handle("sda:connection", (event) => {
  if (event.senderFrame !== event.sender.mainFrame || !isOurPage(event.senderFrame.url)) {
    throw new Error("not allowed");
  }
  return { baseUrl: backend.baseUrl, token: backend.token };
});

/** A throwaway project root for --smoke, so it never touches data/ or workspace/. */
let smokeRootDir = null; // removed when the smoke ends

function smokeRoot() {
  // Empty apart from the fixtures below: config and prompts come from the app
  // itself, exactly as for an installed app's data root (D33).
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sda-smoke-"));
  // A tiny static site with a passing test: a mock run on it succeeds with
  // its tests passed, so it is deployable (D35); the dev smoke deploys it to
  // the in-memory fakes (SDA_FAKE_DEPLOY, D41).
  const site = path.join(root, "workspace", "smoke-site");
  fs.mkdirSync(path.join(site, "tests"), { recursive: true });
  fs.writeFileSync(path.join(site, "index.html"), "<h1>smoke site</h1>\n");
  fs.writeFileSync(path.join(site, "tests", "test_ok.py"), "def test_ok():\n    pass\n");
  // A project that already has tests/, so the pipeline detects a test command
  // and the smoke reaches a real execution gate on the offline mock model.
  // The development smoke rejects that gate; the installed one approves it,
  // so the bundled Python runs this one trivial test.
  const tests = path.join(root, "workspace", "smoke-gates", "tests");
  fs.mkdirSync(tests, { recursive: true });
  fs.writeFileSync(path.join(tests, "test_ok.py"), "def test_ok():\n    pass\n");
  // A corrupt saved-settings file: runs must still work on the defaults, and
  // the UI must say the file was ignored (D31).
  fs.mkdirSync(path.join(root, "data"), { recursive: true });
  fs.writeFileSync(path.join(root, "data", "settings.local.yaml"), "agents: [not valid yaml\n");
  // Hostile content for the file viewer: it must show this as inert text.
  fs.writeFileSync(
    path.join(root, "workspace", "smoke-gates", "notes.md"),
    '# Notes\n<img src=x onerror="document.title=\'pwned\'">\n<script>document.title="pwned"</script>\n[click](javascript:alert(1))\n',
  );
  smokeRootDir = root;
  return root;
}

/**
 * --smoke: run electron/smoke-page.js inside the real page. It checks the
 * security posture (D26) and drives the UI like a person on the offline mock
 * model. Prints each step; exits 0 only if all pass.
 */
async function smoke(win) {
  await new Promise((resolve) => win.webContents.once("did-finish-load", resolve));
  // "installed": the production build, which has no developer menu (D27)
  const mode = app.isPackaged ? "installed" : "dev";
  const script = `const SMOKE_MODE = "${mode}";
${fs.readFileSync(path.join(__dirname, "smoke-page.js"), "utf8")}`;
  const shot = process.env.SDA_SMOKE_SCREENSHOT;
  let dialogShot = null;
  if (shot) {
    // for review: also capture the execution dialog while it is open
    dialogShot = setInterval(async () => {
      const open = await win.webContents
        .executeJavaScript("!!document.querySelector('dialog.approval[open] [data-field=command]')")
        .catch(() => false);
      if (!open || !dialogShot) return;
      clearInterval(dialogShot);
      dialogShot = null;
      fs.writeFileSync(shot.replace(/\.png$/, "-execution.png"), (await win.webContents.capturePage()).toPNG());
    }, 100);
  }
  const report = await win.webContents.executeJavaScript(script);
  if (dialogShot) clearInterval(dialogShot);
  for (const s of report.steps) {
    console.log(`smoke: ${s.ok ? "ok  " : "FAIL"} ${s.name}${s.detail === undefined ? "" : ` (${JSON.stringify(s.detail)})`}`);
  }
  if (report.error) console.log(`smoke: FAIL ${report.error}${report.page ? ` ${JSON.stringify(report.page)}` : ""}`);
  if (process.env.SDA_SMOKE_SCREENSHOT) {
    // for reviewing the screen as the smoke left it
    fs.writeFileSync(process.env.SDA_SMOKE_SCREENSHOT, (await win.webContents.capturePage()).toPNG());
  }
  console.log(`smoke: ${report.ok ? "PASSED" : "FAILED"}`);
  return report.ok ? 0 : 1;
}

app.whenReady().then(async () => {
  if (!DEV_URL) Menu.setApplicationMenu(null); // no DevTools/reload menu in the real app
  lockDownSession();
  try {
    // The dev smoke deploys to in-memory fakes, never to GitHub or Render.
    // An installed app can't honour this: the fakes aren't shipped (D41).
    if (SMOKE && !app.isPackaged) process.env.SDA_FAKE_DEPLOY = "1";
    backend = await startBackend({ root: dataRoot(), ...backendLaunch() });
  } catch (err) {
    console.error(String(err.message || err));
    if (!SMOKE) dialog.showErrorBox("The backend failed to start", String(err.message || err));
    app.exit(1);
    return;
  }
  // A backend that dies later is surfaced too, never silently left dead.
  backend.child.once("exit", (code) => {
    if (quitting) return;
    const detail = `The local API stopped unexpectedly (exit code ${code}).\n\n${backend.stderrTail()}`;
    console.error(detail);
    if (!SMOKE) dialog.showErrorBox("The backend stopped", detail);
    app.exit(1);
  });
  serveDist();
  const win = createWindow();
  if (SMOKE) {
    const code = await smoke(win);
    quitting = true;
    await stopBackend(backend.child);
    // the backend has exited, so nothing holds files in the throwaway root
    fs.rmSync(smokeRootDir, { recursive: true, force: true });
    app.exit(code);
  }
});

app.on("before-quit", (event) => {
  if (quitting || !backend) return;
  // Let the API cancel running commands (D20) before the process goes away.
  event.preventDefault();
  quitting = true;
  stopBackend(backend.child).finally(() => app.quit());
});

app.on("window-all-closed", () => app.quit());
