/**
 * `/internal/verify` against a real Postgres.
 *
 * This endpoint is the platform's trust root: the gateway believes whatever it
 * says, and everything downstream believes the gateway. Its behaviour is
 * therefore tested against a real database and a real BetterAuth instance
 * rather than against mocks — the interesting failures (a revoked session that
 * still verifies, an API key that resolves to the wrong tenant) all live in
 * that integration.
 *
 * Skipped automatically when no database is reachable, so `npm test` works on
 * a laptop with nothing running:
 *
 *     make up && npm --prefix services/auth test
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Hono } from "hono";
import { Pool } from "pg";
import { createAuth } from "../src/auth.js";
import { loadConfig, type Config } from "../src/config.js";
import { registerVerifyRoute } from "../src/verify.js";

const ORIGIN = "http://localhost:8080";

let config: Config;
let pool: Pool;
let app: Hono;
let available = false;

const users = {
  alice: { email: "alice@tenant-a.test", password: "demo-password-alice" },
  bob: { email: "bob@tenant-b.test", password: "demo-password-bob" },
  admin: { email: "admin@platform.test", password: "demo-password-admin" },
};

beforeAll(async () => {
  try {
    config = loadConfig({
      ...process.env,
      DB_HOST: process.env.DB_HOST ?? "127.0.0.1",
      DB_USER: process.env.DB_USER ?? "auth_svc",
      DB_PASSWORD: process.env.DB_PASSWORD ?? "local-auth-password",
      DB_SCHEMA: "auth",
      BETTER_AUTH_SECRET_PATH:
        process.env.BETTER_AUTH_SECRET_PATH ?? "../../dev-keys/better-auth.secret",
      PUBLIC_BASE_URL: ORIGIN,
    });
    pool = new Pool({
      host: config.DB_HOST,
      port: config.DB_PORT,
      database: config.DB_NAME,
      user: config.DB_USER,
      password: config.DB_PASSWORD,
      options: `-c search_path=${config.DB_SCHEMA}`,
      connectionTimeoutMillis: 2000,
    });
    await pool.query('SELECT 1 FROM "user" LIMIT 1');

    const auth = createAuth(config, pool);
    app = new Hono();
    app.on(["GET", "POST"], "/api/auth/*", (c) => auth.handler(c.req.raw));
    registerVerifyRoute(app, auth, pool);
    available = true;
  } catch (error) {
    // No database reachable: `npm test` on a laptop with nothing running should
    // not fail, but it must say so rather than pass silently.
    available = false;
    console.warn(
      `[verify.test] skipping: no database at ${process.env.DB_HOST ?? "127.0.0.1"} ` +
        `(${(error as Error).message}). Run 'make up PROFILE=debug' first.`,
    );
  }
});

afterAll(async () => {
  await pool?.end();
});

/** Sign in and return the session cookie, the way a browser would. */
async function signIn(email: string, password: string): Promise<string> {
  const response = await app.request("/api/auth/sign-in/email", {
    method: "POST",
    headers: { "content-type": "application/json", origin: ORIGIN },
    body: JSON.stringify({ email, password }),
  });
  expect(response.status, await response.text()).toBe(200);
  const cookie = response.headers.get("set-cookie");
  expect(cookie).toBeTruthy();
  return cookie!.split(";")[0]!;
}

async function verify(headers: Record<string, string>) {
  return app.request("/internal/verify", { headers });
}

describe.runIf(process.env.SKIP_DB_TESTS !== "1")("/internal/verify", () => {
  it("rejects a request with no credential at all", async () => {
    if (!available) return;
    const response = await verify({});

    expect(response.status).toBe(401);
  });

  it("rejects a garbage cookie", async () => {
    if (!available) return;
    const response = await verify({ cookie: "better-auth.session_token=not-a-real-token" });

    expect(response.status).toBe(401);
  });

  it("resolves a signed-in user's tenant and role", async () => {
    if (!available) return;
    const cookie = await signIn(users.alice.email, users.alice.password);

    const response = await verify({ cookie });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.user_id).toBeTruthy();
    expect(body.tenant_id).toBe("org_tenant-a");
    expect(body.tenant_role).toBe("owner");
    expect(body.platform_role).toBeNull();
    expect(body.auth_method).toBe("session");
    expect(new Date(body.session_expires_at).getTime()).toBeGreaterThan(Date.now());
  });

  it("reports a member as a member, not as an owner", async () => {
    if (!available) return;
    const cookie = await signIn(users.bob.email, users.bob.password);

    const body = await (await verify({ cookie })).json();

    expect(body.tenant_id).toBe("org_tenant-b");
    expect(body.tenant_role).toBe("member");
  });

  it("reports the platform administrator role", async () => {
    if (!available) return;
    const cookie = await signIn(users.admin.email, users.admin.password);

    const body = await (await verify({ cookie })).json();

    expect(body.platform_role).toBe("admin");
    // The platform admin belongs to no organization.
    expect(body.tenant_id).toBeNull();
    expect(body.tenant_role).toBeNull();
  });

  it("stops accepting a session as soon as it is revoked", async () => {
    if (!available) return;
    const cookie = await signIn(users.alice.email, users.alice.password);
    expect((await verify({ cookie })).status).toBe(200);

    // Revocation is a deleted session row. It must take effect immediately
    // here; the only delay a user sees is the gateway's verify-cache TTL.
    const token = decodeURIComponent(cookie.split("=")[1]!).split(".")[0];
    await pool.query(`DELETE FROM "session" WHERE token = $1`, [token]);

    expect((await verify({ cookie })).status).toBe(401);
  });

  it("rejects an API key that does not exist", async () => {
    if (!available) return;
    const response = await verify({ "x-api-key": "pmp_definitely_not_a_key" });

    expect(response.status).toBe(401);
  });

  it("prefers the API key over a session cookie when both are present", async () => {
    if (!available) return;
    const cookie = await signIn(users.alice.email, users.alice.password);

    const response = await verify({ cookie, "x-api-key": "pmp_definitely_not_a_key" });

    // The key is invalid, so the request fails rather than silently falling
    // back to the cookie — a caller presenting a key means to use it.
    expect(response.status).toBe(401);
  });
});
