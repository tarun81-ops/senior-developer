// npm run smoke:installed: the real installer, end to end (D34).
//   1. install release/<Setup>.exe silently into a temp folder (per user)
//   2. run the installed app with --smoke (security checks, then a run on
//      the offline mock whose execution gate is approved, so the bundled
//      Python runs a real test)
//   3. uninstall silently and delete the folder
// Exits with the smoke's code. Run `npm run dist` first.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DESKTOP = path.resolve(fileURLToPath(import.meta.url), "..", "..");
const pkg = JSON.parse(fs.readFileSync(path.join(DESKTOP, "package.json"), "utf8"));
const product = pkg.build.productName;
const setup = path.join(DESKTOP, "release", `${product} Setup ${pkg.version}.exe`);
if (!fs.existsSync(setup)) {
  console.error(`smoke:installed: ${setup} not found; run npm run dist first`);
  process.exit(1);
}

const wait = (exe, args, opts = {}) => new Promise((resolve, reject) => {
  const child = spawn(exe, args, { stdio: "inherit", windowsVerbatimArguments: true, ...opts });
  child.on("error", reject);
  child.on("exit", (code) => resolve(code ?? 1));
});

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sda-installed-"));
const exe = path.join(dir, `${product}.exe`);
let code = 1;
try {
  console.log(`smoke:installed: installing into ${dir}`);
  // NSIS: /D= must be last and unquoted
  const installed = await wait(`"${setup}"`, ["/S", `/D=${dir}`], { shell: true });
  if (installed !== 0 || !fs.existsSync(exe)) throw new Error(`install failed (exit ${installed})`);
  console.log("smoke:installed: installed; running the installed app's smoke");
  code = await wait(exe, ["--smoke"], { windowsVerbatimArguments: false });
} catch (err) {
  console.error(`smoke:installed: ${err.message}`);
} finally {
  const uninstaller = path.join(dir, `Uninstall ${product}.exe`);
  if (fs.existsSync(uninstaller)) {
    // _?= runs the uninstaller in place, so this waits for it to finish
    const removed = await wait(`"${uninstaller}"`, ["/S", `_?=${dir}`], { shell: true });
    console.log(`smoke:installed: uninstalled (exit ${removed})`);
  }
  fs.rmSync(dir, { recursive: true, force: true });
}
console.log(`smoke:installed: ${code === 0 ? "PASSED" : "FAILED"}`);
process.exit(code);
