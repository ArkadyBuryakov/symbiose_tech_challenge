/**
 * HTTP surface of the auth service.
 *
 * Routes:
 *   /api/auth/*        BetterAuth handler — reached through the gateway, which
 *                      passes these requests through untouched so that every
 *                      `Set-Cookie` survives.
 *   /internal/verify   Identity check for the gateway. Never routed publicly.
 *   /healthz /readyz   Probes.
 *   /metrics           Prometheus exposition.
 */
import { Hono } from "hono";
import type { Pool } from "pg";
import { Registry, collectDefaultMetrics } from "prom-client";
import { createAuth } from "./auth.js";
import type { Config } from "./config.js";
import { pingDatabase } from "./db.js";
import { logger } from "./logger.js";
import { registerVerifyRoute } from "./verify.js";

const registry = new Registry();
collectDefaultMetrics({ register: registry, prefix: "auth_" });

export function createServer(config: Config, pool: Pool): Hono {
  const app = new Hono();
  const auth = createAuth(config, pool);

  // Request id + access log, matching the Python services' field names.
  app.use("*", async (c, next) => {
    const requestId = c.req.header("x-request-id") ?? crypto.randomUUID();
    c.set("requestId" as never, requestId as never);
    const started = performance.now();
    await next();
    c.header("x-request-id", requestId);
    const path = new URL(c.req.url).pathname;
    if (path !== "/healthz" && path !== "/readyz" && path !== "/metrics") {
      logger.info({
        event: "http.request",
        request_id: requestId,
        method: c.req.method,
        path,
        status: c.res.status,
        duration_ms: Math.round((performance.now() - started) * 100) / 100,
      });
    }
  });

  // BetterAuth owns everything under /api/auth. The gateway forwards these
  // requests verbatim — including every Set-Cookie on the way back — because
  // the session cookie is the browser's credential and nothing else may
  // rewrite it.
  app.on(["GET", "POST", "PUT", "DELETE", "OPTIONS"], "/api/auth/*", (c) =>
    auth.handler(c.req.raw),
  );

  // A cheap probe the demo script uses to find out whether auth is wired up.
  app.get("/api/auth/ok", (c) => c.json({ status: "ok" }));

  // Never routed publicly: the gateway is the only caller.
  registerVerifyRoute(app, auth, pool);

  app.get("/healthz", (c) =>
    c.json({ status: "ok", service: "auth", version: config.GIT_SHA }),
  );

  app.get("/readyz", async (c) => {
    try {
      await pingDatabase(pool);
      return c.json({ status: "ok" });
    } catch (error) {
      logger.warn({ event: "readiness.failed", error: (error as Error).message });
      return c.json({ status: "unavailable", detail: (error as Error).message }, 503);
    }
  });

  app.get("/metrics", async (c) =>
    c.text(await registry.metrics(), 200, { "content-type": registry.contentType }),
  );

  app.notFound((c) =>
    c.json(
      {
        type: "https://pmtiles.platform/problems/not-found",
        title: "Not Found",
        status: 404,
        instance: new URL(c.req.url).pathname,
      },
      404,
      { "content-type": "application/problem+json" },
    ),
  );

  return app;
}
