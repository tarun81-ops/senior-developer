import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // relative asset URLs: the built page is served from sda://app/
  base: "./",
  // 127.0.0.1:5173 is the dev origin the API allows (backend/api/security.py)
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
  build: { outDir: "dist", emptyOutDir: true },
});
