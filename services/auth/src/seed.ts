/**
 * Seed the demo tenants, users and producer API key.
 *
 * Idempotent: re-running reconciles rather than duplicating, so `make seed` is
 * safe at any time. It prints the credentials because they are local-only demo
 * accounts — nothing here is a secret, and the README tells the reader so.
 */
import { createAuth, type TenantRole } from "./auth.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";
import { logger } from "./logger.js";
import type { Pool } from "pg";

interface SeedUser {
  email: string;
  password: string;
  name: string;
  organization: string | null;
  role: TenantRole | null;
  platformAdmin?: boolean;
}

const ORGANIZATIONS = [
  { slug: "tenant-a", name: "Tenant A" },
  { slug: "tenant-b", name: "Tenant B" },
];

const USERS: SeedUser[] = [
  {
    email: "alice@tenant-a.test",
    password: "demo-password-alice",
    name: "Alice (Tenant A owner)",
    organization: "tenant-a",
    role: "owner",
  },
  {
    email: "bob@tenant-b.test",
    password: "demo-password-bob",
    name: "Bob (Tenant B member)",
    organization: "tenant-b",
    role: "member",
  },
  {
    email: "admin@platform.test",
    password: "demo-password-admin",
    name: "Platform administrator",
    organization: null,
    role: null,
    platformAdmin: true,
  },
  {
    // The service identity a producing pipeline authenticates as.
    email: "producer@tenant-a.test",
    password: "demo-password-producer",
    name: "Tenant A producer",
    organization: "tenant-a",
    role: "member",
  },
];

const API_KEY_NAME = "tenant-a-producer";

async function userIdByEmail(pool: Pool, email: string): Promise<string | null> {
  const result = await pool.query<{ id: string }>(
    `SELECT id FROM "user" WHERE email = $1 LIMIT 1`,
    [email],
  );
  return result.rows[0]?.id ?? null;
}

async function organizationIdBySlug(pool: Pool, slug: string): Promise<string | null> {
  const result = await pool.query<{ id: string }>(
    `SELECT id FROM "organization" WHERE slug = $1 LIMIT 1`,
    [slug],
  );
  return result.rows[0]?.id ?? null;
}

async function main(): Promise<void> {
  const config = loadConfig();
  const pool = createPool(config);
  const auth = createAuth(config, pool);

  // --- users ------------------------------------------------------------
  // Sign-up goes through BetterAuth, because password hashing is its
  // business and must not be reimplemented here.
  const userIds = new Map<string, string>();
  for (const user of USERS) {
    let userId = await userIdByEmail(pool, user.email);
    if (!userId) {
      await auth.api.signUpEmail({
        body: { email: user.email, password: user.password, name: user.name },
      });
      userId = await userIdByEmail(pool, user.email);
      logger.info({ event: "seed.user_created", email: user.email });
    }
    if (!userId) throw new Error(`could not create user ${user.email}`);
    userIds.set(user.email, userId);

    // No mail server in this environment, so mark the address verified.
    await pool.query(`UPDATE "user" SET "emailVerified" = true WHERE id = $1`, [userId]);
    if (user.platformAdmin) {
      await pool.query(`UPDATE "user" SET role = 'admin' WHERE id = $1`, [userId]);
    }
  }

  // --- organizations and memberships ------------------------------------
  // Written directly rather than through `auth.api.createOrganization`:
  // that endpoint acts on behalf of a *session*, and it makes the caller an
  // owner. The seed needs neither — it needs deterministic ids and a Bob who
  // is only a `member`, so that tenant-role authorization is actually
  // exercised by the tests.
  const organizationIds = new Map<string, string>();
  for (const org of ORGANIZATIONS) {
    let id = await organizationIdBySlug(pool, org.slug);
    if (!id) {
      id = `org_${org.slug}`;
      await pool.query(
        `INSERT INTO "organization" (id, name, slug, "createdAt")
                 VALUES ($1, $2, $3, now())
                 ON CONFLICT (slug) DO NOTHING`,
        [id, org.name, org.slug],
      );
      id = await organizationIdBySlug(pool, org.slug);
      logger.info({ event: "seed.organization_created", slug: org.slug });
    }
    if (!id) throw new Error(`could not create organization ${org.slug}`);
    organizationIds.set(org.slug, id);
  }

  for (const user of USERS) {
    if (!user.organization || !user.role) continue;
    const userId = userIds.get(user.email);
    const organizationId = organizationIds.get(user.organization);
    if (!userId || !organizationId) throw new Error(`cannot place ${user.email}`);

    const existing = await pool.query(
      `SELECT 1 FROM "member" WHERE "userId" = $1 AND "organizationId" = $2`,
      [userId, organizationId],
    );
    if (existing.rowCount === 0) {
      await pool.query(
        `INSERT INTO "member" (id, "organizationId", "userId", role, "createdAt")
                 VALUES ($1, $2, $3, $4, now())`,
        [`mem_${organizationId}_${userId}`, organizationId, userId, user.role],
      );
      logger.info({
        event: "seed.member_added",
        email: user.email,
        organization: user.organization,
        role: user.role,
      });
    } else {
      await pool.query(
        `UPDATE "member" SET role = $3 WHERE "userId" = $1 AND "organizationId" = $2`,
        [userId, organizationId, user.role],
      );
    }
  }

  // Give each tenant member an active organization, so signing in lands them
  // somewhere useful instead of with tenant_id = null.
  for (const user of USERS) {
    const userId = userIds.get(user.email);
    const organizationId = user.organization
      ? organizationIds.get(user.organization)
      : undefined;
    if (!userId || !organizationId) continue;
    await pool.query(
      `UPDATE "session" SET "activeOrganizationId" = $2
             WHERE "userId" = $1 AND "activeOrganizationId" IS NULL`,
      [userId, organizationId],
    );
  }

  // --- producer API key -------------------------------------------------
  const producerId = userIds.get("producer@tenant-a.test") ?? null;
  const tenantAId = organizationIds.get("tenant-a");
  if (!producerId || !tenantAId) throw new Error("producer user or tenant-a is missing");

  let apiKeyValue: string | null = null;
  // Keys are hashed at rest, so an existing one can never be shown again.
  // ROTATE_API_KEY=1 deletes it and mints a replacement — which is also the
  // supported way to revoke a leaked producer key.
  if (process.env.ROTATE_API_KEY === "1") {
    await pool.query(`DELETE FROM "apikey" WHERE "referenceId" = $1 AND name = $2`, [
      producerId,
      API_KEY_NAME,
    ]);
    logger.info({ event: "seed.api_key_rotated", name: API_KEY_NAME });
  }

  const existingKey = await pool.query(
    `SELECT 1 FROM "apikey" WHERE "referenceId" = $1 AND name = $2`,
    [producerId, API_KEY_NAME],
  );
  if (existingKey.rowCount === 0) {
    const created = await auth.api.createApiKey({
      body: {
        userId: producerId,
        name: API_KEY_NAME,
        // Binds the key to one organization; `/internal/verify` reads
        // this to decide which tenant the producer acts as.
        metadata: { tenant_id: tenantAId },
      },
    });
    apiKeyValue = created?.key ?? null;
  }

  await pool.end();
  print(organizationIds, apiKeyValue);
}

function print(organizationIds: Map<string, string>, apiKey: string | null): void {
  const line = "─".repeat(72);
  const rows = USERS.map((u) => ({
    email: u.email,
    password: u.password,
    tenant: u.organization ?? "—",
    role: u.platformAdmin ? "platform admin" : (u.role ?? "—"),
  }));

  process.stdout.write(
    `\n${line}\nDemo accounts (local only — these are not secrets)\n${line}\n`,
  );
  for (const row of rows) {
    process.stdout.write(
      `  ${row.email.padEnd(26)} ${row.password.padEnd(24)} ${row.tenant.padEnd(10)} ${row.role}\n`,
    );
  }
  process.stdout.write(`${line}\nOrganizations\n`);
  for (const [slug, id] of organizationIds) {
    process.stdout.write(`  ${slug.padEnd(12)} ${id}\n`);
  }
  process.stdout.write(`${line}\nProducer API key (tenant-a)\n`);
  if (apiKey) {
    process.stdout.write(`  ${apiKey}\n`);
    process.stdout.write(`  Use it as:  x-api-key: ${apiKey}\n`);
    process.stdout.write(
      "  This is the only time it is shown; re-run `make seed-rotate`\n" +
        "  to mint a new one.\n",
    );
  } else {
    process.stdout.write(
      "  (already exists — the value is hashed at rest and cannot be shown again)\n",
    );
  }
  process.stdout.write(`${line}\n\n`);
}

main().catch((error: unknown) => {
  // BetterAuth throws APIError, whose useful detail is in `body`/`status`
  // rather than in `message` — surface all of it or seeding failures are
  // impossible to diagnose.
  const err = error as Error & { status?: unknown; body?: unknown; cause?: unknown };
  logger.fatal({
    event: "seed.failed",
    error: err.message || String(error),
    status: err.status,
    body: err.body,
    cause: err.cause instanceof Error ? err.cause.message : err.cause,
    stack: err.stack,
  });
  process.exit(1);
});
