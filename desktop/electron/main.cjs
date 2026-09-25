// Electron main process (D25, D26): starts the backend, serves the UI from
// sda://app, and keeps the one window locked down.

const { app, BrowserWindow, Menu, dialog, ipcMain, net, protocol, session } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { REPO_ROOT, startBackend, stopBackend } = require("./backend.cjs");

// Must match APP_ORIGIN in backend/api/security.py exactly.
const SCHEME = "sda";
const APP_ORIGIN = `${SCHEME}://app`;
const DIST = path.join(__dirname, "..", "dist");
const DEV_URL = process.env.SDA_DEV_URL; // set only by scripts/dev.mjs
const SMOKE = process.argv.includes("--smoke");

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
    show: !SMOKE,
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
function smokeRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sda-smoke-"));
  fs.cpSync(path.join(REPO_ROOT, "config"), path.join(root, "config"), { recursive: true });
  return root;
}

/**
 * --smoke: load the built UI from sda://app and check, inside the real page,
 * that it connected, has no Node, sees a bridge of exactly one function, and
 * is held to the CSP. Exits 0 or 1.
 */
async function smoke(win) {
  await new Promise((resolve) => win.webContents.once("did-finish-load", resolve));
  const report = await win.webContents.executeJavaScript(`
    new Promise((resolve) => {
      const started = Date.now();
      (function check() {
        const el = document.querySelector("[data-connection]");
        const state = el && el.dataset.connection;
        if (state !== "ok" && state !== "error" && Date.now() - started < 20000) {
          return setTimeout(check, 100);
        }
        // A request to any other origin must be stopped by the CSP before it
        // leaves the page (port 1 on loopback: nothing real is contacted).
        let violated = null;
        document.addEventListener("securitypolicyviolation", (e) => { violated = e.violatedDirective; });
        fetch("http://127.0.0.1:1/").catch(() => {}).finally(() => setTimeout(() => resolve({
          state,
          text: el && el.textContent,
          origin: location.origin,
          node: typeof require !== "undefined" || typeof process !== "undefined",
          bridge: Object.keys(window.sda || {}),
          cspBlockedOtherOrigin: violated,
        }), 100));
      })();
    })`);
  console.log("smoke:", JSON.stringify(report));
  const ok = report.state === "ok"
    && report.origin === APP_ORIGIN
    && report.node === false
    && JSON.stringify(report.bridge) === '["connection"]'
    && report.cspBlockedOtherOrigin === "connect-src";
  return ok ? 0 : 1;
}

app.whenReady().then(async () => {
  if (!DEV_URL) Menu.setApplicationMenu(null); // no DevTools/reload menu in the real app
  lockDownSession();
  try {
    backend = await startBackend({ root: process.env.SDA_ROOT || (SMOKE ? smokeRoot() : undefined) });
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
