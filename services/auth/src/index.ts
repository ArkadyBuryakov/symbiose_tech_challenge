/**
 * Auth service entry point.
 *
 * Phase 0 shape: configuration, database pool, probes and metrics. The
 * BetterAuth handler and `/internal/verify` are mounted in `server.ts`.
 */
import { serve } from "@hono/node-server";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";
import { logger } from "./logger.js";
import { createServer } from "./server.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const pool = createPool(config);
  const app = createServer(config, pool);

  const server = serve(
    { fetch: app.fetch, hostname: config.HTTP_HOST, port: config.HTTP_PORT },
    (info) => {
      logger.info({
        event: "auth.started",
        port: info.port,
        environment: config.ENVIRONMENT,
        base_url: config.baseUrl,
      });
    },
  );

  // Graceful shutdown: stop accepting connections, then drain the pool. The
  // gateway retries, so in-flight verifies are not lost.
  const shutdown = (signal: string) => {
    logger.info({ event: "auth.shutdown", signal });
    server.close(() => {
      void pool.end().then(() => process.exit(0));
    });
    setTimeout(() => process.exit(1), 10_000).unref();
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

main().catch((error: unknown) => {
  logger.fatal({ event: "auth.startup_failed", error: (error as Error).message });
  process.exit(1);
});
