/**
 * Postgres pool for the auth service.
 *
 * The pool's `search_path` is pinned to the `auth` schema, which is the only
 * schema the `auth_svc` role can touch. BetterAuth therefore creates and reads
 * all of its tables there without knowing anything about the schema split.
 */
import { Pool } from "pg";
import type { Config } from "./config.js";
import { logger } from "./logger.js";

export function createPool(config: Config): Pool {
  const pool = new Pool({
    host: config.DB_HOST,
    port: config.DB_PORT,
    database: config.DB_NAME,
    user: config.DB_USER,
    password: config.DB_PASSWORD,
    ssl: config.DB_SSLMODE === "disable" ? false : { rejectUnauthorized: false },
    max: config.DB_POOL_MAX,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
    application_name: "pmp-auth",
    options: `-c search_path=${config.DB_SCHEMA}`,
  });

  pool.on("error", (error) => {
    logger.error({ event: "db.pool_error", error: error.message });
  });

  return pool;
}

export async function pingDatabase(pool: Pool): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("SELECT 1");
  } finally {
    client.release();
  }
}
