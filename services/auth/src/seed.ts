/**
 * Seed the demo tenants, users and producer API key.
 *
 * Idempotent: re-running reconciles rather than duplicating, so `make seed` is
 * safe at any time. It prints the credentials because they are local-only demo
 * accounts — nothing here is a secret, and the README tells the reader so.
 */
import { existsSync, writeFileSync } from "node:fs";
import { createAuth, type TenantRole } from "./auth.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";
import { logger } from "./logger.js";
import { addTenant, addUser } from "./users-cli.js";

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

async function main(): Promise<void> {
  const config = loadConfig();
  const pool = createPool(config);
  const auth = createAuth(config, pool);

  // Deterministic ids (`org_<slug>`) and a Bob who is only a `member`, so that
  // tenant-role authorization is actually exercised by the tests.
  const organizationIds = new Map<string, string>();
  for (const org of ORGANIZATIONS) {
    organizationIds.set(org.slug, await addTenant(pool, org.slug, org.name));
  }

  const userIds = new Map<string, string>();
  for (const user of USERS) {
    const userId = await addUser(pool, auth, {
      email: user.email,
      password: user.password,
      name: user.name,
      tenantId: user.organization ? (organizationIds.get(user.organization) ?? null) : null,
      role: user.role ?? "member",
      platformAdmin: user.platformAdmin ?? false,
    });
    userIds.set(user.email, userId);
  }
  logger.info({ event: "seed.users_ready", count: USERS.length });

  // --- producer API key -------------------------------------------------
  // Keys are hashed at rest, so a key can only be shown when it is minted.
  // It is written to PRODUCER_API_KEY_PATH (dev-keys/producer-api-key via
  // `make seed`) for `make demo` and the e2e suite. If the key exists but the
  // file does not, the key is replaced: an unrecoverable key is useless.
  const producerId = userIds.get("producer@tenant-a.test");
  const tenantAId = organizationIds.get("tenant-a");
  if (!producerId || !tenantAId) throw new Error("producer user or tenant-a is missing");

  const keyPath = process.env.PRODUCER_API_KEY_PATH;
  const existingKey = await pool.query(
    `SELECT 1 FROM "apikey" WHERE "referenceId" = $1 AND name = $2`,
    [producerId, API_KEY_NAME],
  );
  let apiKeyValue: string | null = null;
  if (existingKey.rowCount === 0 || (keyPath && !existsSync(keyPath))) {
    await pool.query(`DELETE FROM "apikey" WHERE "referenceId" = $1 AND name = $2`, [
      producerId,
      API_KEY_NAME,
    ]);
    const created = await auth.api.createApiKey({
      body: {
        userId: producerId,
        name: API_KEY_NAME,
        // Selects the organization the producer acts as; `/internal/verify`
        // honours it only because the producer is a member of it.
        metadata: { tenant_id: tenantAId },
      },
    });
    apiKeyValue = created.key;
    if (keyPath) writeFileSync(keyPath, `${apiKeyValue}\n`, { mode: 0o644 });
    logger.info({ event: "seed.api_key_minted", name: API_KEY_NAME, file: keyPath ?? null });
  }

  await pool.end();
  print(organizationIds, apiKeyValue, keyPath);
}

function print(
  organizationIds: Map<string, string>,
  apiKey: string | null,
  keyPath: string | undefined,
): void {
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
  } else {
    process.stdout.write("  (unchanged — the value is hashed at rest)\n");
  }
  if (keyPath)
    process.stdout.write(`  Stored in ${keyPath}; delete the file to mint a new key.\n`);
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
