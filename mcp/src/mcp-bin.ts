import { serve } from "./index.js";

const start = process.argv.includes("--http")
  ? import("./http.js").then(({ startHttpServer }) => startHttpServer())
  : serve();

start.catch((error: unknown) => {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(`code-search-mcp fatal: ${msg}`);
  process.exit(1);
});
