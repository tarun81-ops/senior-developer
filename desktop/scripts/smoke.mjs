// npm run smoke: build the UI in development mode (the smoke drives the
// offline mock model, which only the development-only menu offers, D27), then
// run Electron's --smoke check against it. Exits with Electron's code.
// `npm start` always rebuilds for production, so this build never ships.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { build } from "vite";

process.env.NODE_ENV = "development"; // Vite derives import.meta.env.DEV from this
await build({
  configFile: fileURLToPath(new URL("../vite.config.js", import.meta.url)),
  mode: "development",
  logLevel: "warn",
});

const electron = createRequire(import.meta.url)("electron");
const child = spawn(electron, [".", "--smoke"], { stdio: "inherit" });
child.on("exit", (code) => process.exit(code ?? 1));
