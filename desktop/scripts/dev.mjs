// npm run dev: Vite with hot reload on 127.0.0.1:5173 (an allowed origin),
// and Electron pointed at it. Closing the window stops both.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const configFile = fileURLToPath(new URL("../vite.config.js", import.meta.url));
const server = await createServer({ configFile });
await server.listen();

const electron = createRequire(import.meta.url)("electron"); // path to the binary
const child = spawn(electron, ["."], {
  stdio: "inherit",
  env: { ...process.env, SDA_DEV_URL: "http://127.0.0.1:5173" },
});
child.on("exit", async (code) => {
  await server.close();
  process.exit(code ?? 0);
});
