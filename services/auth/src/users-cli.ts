/**
 * Operator CLI for tenants and users, and the provisioning helpers the seed
 * reuses.
 *
 *   node dist/users-cli.js add-tenant <slug> [name]
 *   node dist/users-cli.js add-user <email> <password> [--tenant <slug>] [--role owner|admin|member]
 *                                    [--name <display name>] [--platform-admin]
 *
 * Without --tenant, the user joins the tenant named after their email domain
 * (a@acme.com -> `acme-com`), created if missing. Without --role, the tenant's
 * first member becomes its owner and later ones members.
 *   node dist/users-cli.js list
 *
 * Wrapped by `make add-tenant`, `make add-user`, `make aws-add-user` and
 * `make list-users`.
 *
 * Public sign-up is disabled, so users are created with the admin plugin's
 * server-side `createUser` (BetterAuth owns password hashing). Memberships are
 * written directly: the organization endpoints act on behalf of a session and
 * make the caller an owner. Re-running is safe — an existing user keeps their
 * password and just gains the membership/role requested.
 */
import type { Pool } from "pg";
import { pathToFileURL } from "node:url";
import { createAuth, TENANT_ROLES, type Auth, type TenantRole } from "./auth.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";

export interface NewUser {
  email: string;
  password: string;
  name: string;
  /** Organization id (not slug) to add the user to, if any. */
  tenantId: string | null;
  /** null: owner if the tenant has no members yet, else member; an existing role is kept. */
  role: TenantRole | null;
  platformAdmin: boolean;
}

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

export async function organizationId(pool: Pool, slug: string): Promise<string | null> {
  const r = await pool.query<{ id: string }>(`SELECT id FROM "organization" WHERE slug = $1`, [
    slug,
  ]);
  return r.rows[0]?.id ?? null;
}

/** Create the tenant if it does not exist; returns its id (`org_<slug>`). */
export async function addTenant(pool: Pool, slug: string, name: string): Promise<string> {
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(slug)) throw new Error(`invalid tenant slug '${slug}'`);
  const existing = await organizationId(pool, slug);
  if (existing) return existing;
  const id = `org_${slug}`;
  await pool.query(
    `INSERT INTO "organization" (id, name, slug, "createdAt") VALUES ($1, $2, $3, now())`,
    [id, name, slug],
  );
  return id;
}

/** Create the user if missing, then apply the platform role and membership. */
export async function addUser(pool: Pool, auth: Auth, user: NewUser): Promise<string> {
  const found = await pool.query<{ id: string }>(`SELECT id FROM "user" WHERE email = $1`, [
    user.email.toLowerCase(),
  ]);
  let userId = found.rows[0]?.id;
  if (!userId) {
    const created = await auth.api.createUser({
      body: {
        email: user.email,
        password: user.password,
        name: user.name,
        role: user.platformAdmin ? "admin" : "user",
        // No mail server in this environment.
        data: { emailVerified: true },
      },
    });
    userId = created.user.id;
  } else if (user.platformAdmin) {
    await pool.query(`UPDATE "user" SET role = 'admin' WHERE id = $1`, [userId]);
  }

  if (user.tenantId) {
    await pool.query(
      `INSERT INTO "member" (id, "organizationId", "userId", role, "createdAt")
       VALUES ($1, $2, $3, COALESCE($4, CASE WHEN EXISTS
                 (SELECT 1 FROM "member" WHERE "organizationId" = $2) THEN 'member' ELSE 'owner' END),
               now())
       ON CONFLICT (id) DO UPDATE SET role = COALESCE($4, "member".role)`,
      [`mem_${user.tenantId}_${userId}`, user.tenantId, userId, user.role],
    );
  }
  return userId;
}

async function addUserCommand(pool: Pool, auth: Auth, args: string[]): Promise<void> {
  const { positional, flags } = parseFlags(args);
  const [email, password] = positional;
  if (!email || !password) usage("add-user needs <email> <password>");
  if (password.length < 10) usage("password must be at least 10 characters");

  const role = typeof flags.role === "string" ? (flags.role as TenantRole) : null;
  if (role && !TENANT_ROLES.includes(role))
    usage(`role must be one of ${TENANT_ROLES.join(", ")}`);

  let tenant: string;
  let tenantId: string | null;
  if (typeof flags.tenant === "string") {
    tenant = flags.tenant;
    tenantId = await organizationId(pool, tenant);
    if (!tenantId) usage(`tenant '${tenant}' does not exist — create it with add-tenant first`);
  } else {
    const domain = email.split("@")[1]?.toLowerCase();
    if (!domain) usage("email has no domain");
    tenant = domain.replace(/[^a-z0-9]+/g, "-").slice(0, 63);
    tenantId = await addTenant(pool, tenant, domain);
  }

  const platformAdmin = flags["platform-admin"] === true;
  const userId = await addUser(pool, auth, {
    email,
    password,
    name: typeof flags.name === "string" ? flags.name : email.split("@")[0]!,
    tenantId,
    role,
    platformAdmin,
  });
  const member = await pool.query<{ role: string }>(
    `SELECT role FROM "member" WHERE "organizationId" = $1 AND "userId" = $2`,
    [tenantId, userId],
  );
  process.stdout.write(`user ${email} ready\n`);
  if (platformAdmin) process.stdout.write("  platform admin\n");
  process.stdout.write(`  ${member.rows[0]?.role} of tenant '${tenant}'\n`);
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
        const id = await addTenant(pool, slug, name.join(" ") || slug);
        process.stdout.write(`tenant '${slug}' ready (${id})\n`);
        break;
      }
      case "add-user":
        await addUserCommand(pool, createAuth(config, pool), args);
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

// Run only when executed directly; the seed imports the helpers above.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error: unknown) => {
    const err = error as Error & { body?: unknown };
    process.stderr.write(`error: ${err.message} ${err.body ? JSON.stringify(err.body) : ""}\n`);
    process.exit(1);
  });
}
