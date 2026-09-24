/**
 * Operator CLI for tenants and users.
 *
 *   node dist/users-cli.js add-tenant <slug> [name]
 *   node dist/users-cli.js add-user <email> <password> [--tenant <slug>] [--role owner|admin|member]
 *                                    [--name <display name>] [--platform-admin]
 *   node dist/users-cli.js list
 *
 * Wrapped by `make add-tenant`, `make add-user` and `make list-users`.
 *
 * Users are created through BetterAuth (it owns password hashing);
 * memberships are written directly, for the same reason as in the seed: the
 * organization endpoints act on behalf of a session and make the caller an
 * owner. Re-running is safe — an existing user keeps their password and just
 * gains the membership/role requested.
 */
import type { Pool } from "pg";
import { createAuth, TENANT_ROLES, type TenantRole } from "./auth.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";

function usage(message?: string): never {
  if (message) process.stderr.write(`error: ${message}\n\n`);
  process.stderr.write(
    [
      "usage:",
      "  add-tenant <slug> [name]",
      "  add-user <email> <password> [--tenant <slug>] [--role owner|admin|member]",
      "           [--name <display name>] [--platform-admin]",
      "  list",
      "",
    ].join("\n"),
  );
  process.exit(2);
}

function parseFlags(args: string[]): {
  positional: string[];
  flags: Record<string, string | true>;
} {
  const positional: string[] = [];
  const flags: Record<string, string | true> = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i]!;
    if (arg.startsWith("--")) {
      const next = args[i + 1];
      if (next === undefined || next.startsWith("--")) flags[arg.slice(2)] = true;
      else {
        flags[arg.slice(2)] = next;
        i += 1;
      }
    } else positional.push(arg);
  }
  return { positional, flags };
}

async function organizationId(pool: Pool, slug: string): Promise<string | null> {
  const r = await pool.query<{ id: string }>(`SELECT id FROM "organization" WHERE slug = $1`, [
    slug,
  ]);
  return r.rows[0]?.id ?? null;
}

async function addTenant(pool: Pool, slug: string, name: string): Promise<string> {
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(slug)) usage(`invalid tenant slug '${slug}'`);
  const existing = await organizationId(pool, slug);
  if (existing) {
    process.stdout.write(`tenant '${slug}' already exists (${existing})\n`);
    return existing;
  }
  const id = `org_${slug}`;
  await pool.query(
    `INSERT INTO "organization" (id, name, slug, "createdAt") VALUES ($1, $2, $3, now())`,
    [id, name, slug],
  );
  process.stdout.write(`created tenant '${slug}' (${id})\n`);
  return id;
}

async function addUser(
  pool: Pool,
  auth: ReturnType<typeof createAuth>,
  args: string[],
): Promise<void> {
  const { positional, flags } = parseFlags(args);
  const [email, password] = positional;
  if (!email || !password) usage("add-user needs <email> <password>");
  if (password.length < 10) usage("password must be at least 10 characters");

  const role = (flags.role ?? "member") as TenantRole;
  if (!TENANT_ROLES.includes(role)) usage(`role must be one of ${TENANT_ROLES.join(", ")}`);
  const tenant = typeof flags.tenant === "string" ? flags.tenant : null;
  const name = typeof flags.name === "string" ? flags.name : email.split("@")[0]!;

  let tenantId: string | null = null;
  if (tenant) {
    tenantId = await organizationId(pool, tenant);
    if (!tenantId) usage(`tenant '${tenant}' does not exist — create it with add-tenant first`);
  }

  const found = await pool.query<{ id: string }>(`SELECT id FROM "user" WHERE email = $1`, [
    email,
  ]);
  let userId = found.rows[0]?.id;
  if (userId) {
    process.stdout.write(`user ${email} already exists; password unchanged\n`);
  } else {
    await auth.api.signUpEmail({ body: { email, password, name } });
    userId = (
      await pool.query<{ id: string }>(`SELECT id FROM "user" WHERE email = $1`, [email])
    ).rows[0]?.id;
    if (!userId) throw new Error(`could not create ${email}`);
    // No mail server in this environment.
    await pool.query(`UPDATE "user" SET "emailVerified" = true WHERE id = $1`, [userId]);
    process.stdout.write(`created user ${email}\n`);
  }

  if (flags["platform-admin"]) {
    await pool.query(`UPDATE "user" SET role = 'admin' WHERE id = $1`, [userId]);
    process.stdout.write(`  granted platform admin\n`);
  }

  if (tenantId) {
    await pool.query(
      `INSERT INTO "member" (id, "organizationId", "userId", role, "createdAt")
       VALUES ($1, $2, $3, $4, now())
       ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role`,
      [`mem_${tenantId}_${userId}`, tenantId, userId, role],
    );
    process.stdout.write(`  ${role} of tenant '${tenant}'\n`);
  } else if (!flags["platform-admin"]) {
    process.stdout.write(
      "  note: no --tenant given; this user can sign in and read public datasets,\n" +
        "  but cannot publish until added to a tenant.\n",
    );
  }
}

async function list(pool: Pool): Promise<void> {
  const r = await pool.query<{ email: string; admin: boolean; tenants: string | null }>(
    `SELECT u.email, (u.role = 'admin') AS admin,
            string_agg(o.slug || ':' || m.role, ', ' ORDER BY o.slug) AS tenants
       FROM "user" u
       LEFT JOIN "member" m ON m."userId" = u.id
       LEFT JOIN "organization" o ON o.id = m."organizationId"
      GROUP BY u.email, u.role ORDER BY u.email`,
  );
  for (const row of r.rows) {
    const extra = [row.tenants, row.admin ? "platform admin" : null].filter(Boolean).join("; ");
    process.stdout.write(`  ${row.email.padEnd(32)} ${extra || "—"}\n`);
  }
}

async function main(): Promise<void> {
  const [command, ...args] = process.argv.slice(2);
  const config = loadConfig();
  const pool = createPool(config);
  try {
    switch (command) {
      case "add-tenant": {
        const [slug, ...name] = args;
        if (!slug) usage("add-tenant needs <slug>");
        await addTenant(pool, slug, name.join(" ") || slug);
        break;
      }
      case "add-user":
        await addUser(pool, createAuth(config, pool), args);
        break;
      case "list":
        await list(pool);
        break;
      default:
        usage(command ? `unknown command '${command}'` : undefined);
    }
  } finally {
    await pool.end();
  }
}

main().catch((error: unknown) => {
  const err = error as Error & { body?: unknown };
  process.stderr.write(`error: ${err.message} ${err.body ? JSON.stringify(err.body) : ""}\n`);
  process.exit(1);
});
