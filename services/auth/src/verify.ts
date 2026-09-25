/**
 * `GET /internal/verify` — the gateway's identity check.
 *
 * This is the *only* endpoint the gateway calls directly, and it is never
 * routed publicly. It answers one question: given this session cookie or this
 * API key, who is the caller and what are they in this organization?
 *
 * The gateway caches successful answers briefly (default 10s) and never caches
 * failures, so this endpoint is also where session revocation takes effect:
 * BetterAuth's cookie cache is deliberately disabled so every call reads the
 * session row.
 */
import type { Context } from "hono";
import type { Pool } from "pg";
import type { Auth, TenantRole } from "./auth.js";
import { logger } from "./logger.js";

export interface VerifiedIdentity {
  user_id: string;
  tenant_id: string | null;
  tenant_role: TenantRole | null;
  platform_role: "admin" | null;
  auth_method: "session" | "api_key";
  session_expires_at: string | null;
}

const API_KEY_HEADER = "x-api-key";

/**
 * A user's role inside one organization.
 *
 * Read straight from the organization plugin's `member` table: the session
 * carries the *active* organization but not the caller's role in it, and the
 * gateway needs the role to enforce route policies.
 */
async function tenantRoleOf(
  pool: Pool,
  userId: string,
  organizationId: string | null,
): Promise<TenantRole | null> {
  if (!organizationId) return null;
  const result = await pool.query<{ role: string }>(
    `SELECT role FROM "member" WHERE "userId" = $1 AND "organizationId" = $2 LIMIT 1`,
    [userId, organizationId],
  );
  const role = result.rows[0]?.role;
  if (!role) return null;
  // The plugin stores multiple roles as a comma-separated string; the most
  // privileged one is what the platform enforces against.
  const roles = role.split(",").map((r) => r.trim());
  for (const candidate of ["owner", "admin", "member"] as const) {
    if (roles.includes(candidate)) return candidate;
  }
  return null;
}

async function platformRoleOf(pool: Pool, userId: string): Promise<"admin" | null> {
  const result = await pool.query<{ role: string | null }>(
    `SELECT role FROM "user" WHERE id = $1 LIMIT 1`,
    [userId],
  );
  return result.rows[0]?.role === "admin" ? "admin" : null;
}

/**
 * The organization a caller acts as when none is pinned on the session or key.
 *
 * A user who belongs to exactly one organization should not have to choose it
 * before the API will talk to them, and an API key with a tenant in its
 * metadata should not need a session at all. Ambiguity — more than one
 * membership and no explicit choice — is refused rather than guessed: picking
 * one would silently decide which tenant's data a request touches.
 */
async function soleOrganizationOf(pool: Pool, userId: string): Promise<string | null> {
  const result = await pool.query<{ organizationId: string }>(
    `SELECT "organizationId" FROM "member" WHERE "userId" = $1 LIMIT 2`,
    [userId],
  );
  return result.rows.length === 1 ? (result.rows[0]?.organizationId ?? null) : null;
}

/** The organization an API-key caller acts as. */
async function tenantOfApiKey(
  pool: Pool,
  key: { referenceId?: string | null; metadata?: unknown },
): Promise<string | null> {
  const metadata = key.metadata as { tenant_id?: string } | null | undefined;
  if (metadata?.tenant_id) return metadata.tenant_id;
  if (!key.referenceId) return null;
  return soleOrganizationOf(pool, key.referenceId);
}

export function registerVerifyRoute(app: { get: Function }, auth: Auth, pool: Pool): void {
  app.get("/internal/verify", async (c: Context) => {
    const requestId = c.req.header("x-request-id") ?? "";
    const presentedKey = c.req.header(API_KEY_HEADER);

    try {
      const identity = presentedKey
        ? await verifyApiKey(auth, pool, presentedKey)
        : await verifySession(auth, pool, c.req.raw.headers);

      if (!identity) {
        // No detail: an unauthenticated caller learns only that it failed.
        return c.json({ error: "unauthenticated" }, 401);
      }
      return c.json(identity satisfies VerifiedIdentity);
    } catch (error) {
      logger.error({
        event: "verify.failed",
        request_id: requestId,
        method: presentedKey ? "api_key" : "session",
        error: (error as Error).message,
      });
      return c.json({ error: "verification_failed" }, 503);
    }
  });
}

async function verifySession(
  auth: Auth,
  pool: Pool,
  headers: Headers,
): Promise<VerifiedIdentity | null> {
  // Returns null for a missing, expired or revoked session.
  const result = await auth.api.getSession({ headers });
  if (!result?.user || !result.session) return null;

  const session = result.session as typeof result.session & {
    activeOrganizationId?: string | null;
  };
  // An explicit choice always wins; otherwise fall back to the user's sole
  // membership, so a single-tenant user does not have to pick before the API
  // will talk to them.
  const tenantId =
    session.activeOrganizationId ?? (await soleOrganizationOf(pool, result.user.id));

  const tenantRole = await tenantRoleOf(pool, result.user.id, tenantId);
  // The admin plugin puts the platform role on the user row, which getSession
  // already returned.
  const user = result.user as typeof result.user & { role?: string | null };

  return {
    user_id: result.user.id,
    // A signed-in user with no active organization is a valid identity; the
    // backend is what refuses them on tenant-scoped endpoints. A tenant the
    // user is not (or no longer) a member of is treated the same way.
    tenant_id: tenantRole ? tenantId : null,
    tenant_role: tenantRole,
    platform_role: user.role === "admin" ? "admin" : null,
    auth_method: "session",
    session_expires_at: new Date(session.expiresAt).toISOString(),
  };
}

async function verifyApiKey(
  auth: Auth,
  pool: Pool,
  presentedKey: string,
): Promise<VerifiedIdentity | null> {
  const result = await auth.api.verifyApiKey({ body: { key: presentedKey } });
  if (!result.valid || !result.key) return null;

  const key = result.key as { referenceId?: string | null; metadata?: unknown };
  const userId = key.referenceId;
  if (!userId) return null;

  const tenantId = await tenantOfApiKey(pool, key);
  const [tenantRole, platformRole] = await Promise.all([
    tenantRoleOf(pool, userId, tenantId),
    platformRoleOf(pool, userId),
  ]);

  return {
    user_id: userId,
    // Key metadata is client-writable (anyone signed in can create a key with
    // any metadata), so it only *selects* a tenant: the key's owner must be a
    // member of it, or the key acts as nobody's tenant.
    tenant_id: tenantRole ? tenantId : null,
    tenant_role: tenantRole,
    platform_role: platformRole,
    auth_method: "api_key",
    // API keys have their own expiry; there is no session to report.
    session_expires_at: null,
  };
}
