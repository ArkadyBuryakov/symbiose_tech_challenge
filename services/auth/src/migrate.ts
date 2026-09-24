/**
 * One-shot migration for the `auth` schema.
 *
 * BetterAuth owns the shape of its own tables, so its own migration planner is
 * the only thing that knows them. This uses the programmatic API rather than
 * `@better-auth/cli`, which lags the library's version and would have to
 * re-discover the config from a file.
 *
 * The pool pins `search_path` to the `auth` schema, so every table lands there
 * and the `auth_svc` role (which can see nothing else) can use them.
 */
import { getMigrations } from "better-auth/db/migration";
import { createAuth } from "./auth.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";
import { logger } from "./logger.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const pool = createPool(config);
  const auth = createAuth(config, pool);

  const plan = await getMigrations(auth.options);

  if (plan.schemaProblems.length > 0) {
    for (const problem of plan.schemaProblems) {
      logger.error({ event: "auth.migrate.schema_problem", problem });
    }
    throw new Error("BetterAuth reported schema problems; refusing to migrate");
  }
  if (plan.unsafeChanges.length > 0) {
    // Adding a required column with no default to a populated table needs a
    // deliberate backfill, not an automatic migration.
    for (const change of plan.unsafeChanges) {
      logger.error({ event: "auth.migrate.unsafe_change", change });
    }
    throw new Error("BetterAuth migration would be unsafe; resolve it by hand");
  }

  const created = plan.toBeCreated.map((t) => t.table);
  const altered = plan.toBeAdded.map((t) => t.table);

  if (created.length === 0 && altered.length === 0 && plan.toBeAddedIndexes.length === 0) {
    logger.info({ event: "auth.migrate.up_to_date", schema: config.DB_SCHEMA });
  } else {
    logger.info({
      event: "auth.migrate.applying",
      schema: config.DB_SCHEMA,
      create_tables: created,
      alter_tables: altered,
      add_indexes: plan.toBeAddedIndexes.length,
    });
    await plan.runMigrations();
    logger.info({ event: "auth.migrate.done" });
  }

  // Tables are created by `auth_svc` itself, so it already owns them; the
  // grant is here so a future migration run as a different role still leaves
  // the service able to use them.
  await pool.query(
    `GRANT USAGE ON SCHEMA "${config.DB_SCHEMA}" TO auth_svc;
         GRANT SELECT, INSERT, UPDATE, DELETE
           ON ALL TABLES IN SCHEMA "${config.DB_SCHEMA}" TO auth_svc;`,
  );

  await pool.end();
}

main().catch((error: unknown) => {
  logger.fatal({ event: "auth.migrate.failed", error: (error as Error).message });
  process.exit(1);
});
