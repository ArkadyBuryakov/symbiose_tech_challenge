/**
 * Postgres pool for the auth service.
 *
 * The pool's `search_path` is pinned to the `auth` schema, which is the only
 * schema the `auth_svc` role can touch. BetterAuth therefore creates and reads
 * all of its tables there without knowing anything about the schema split.
 */
import { Signer } from "@aws-sdk/rds-signer";
import { Pool } from "pg";
import type { Config } from "./config.js";
import { logger } from "./logger.js";

export function createPool(config: Config): Pool {
  const pool = new Pool({
    host: config.DB_HOST,
    port: config.DB_PORT,
    database: config.DB_NAME,
    user: config.DB_USER,
    password: config.DB_AUTH === "iam" ? iamTokenProvider(config) : config.DB_PASSWORD,
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

/**
 * RDS IAM authentication: `pg` calls this for every new connection, and the
 * signer produces a token valid for 15 minutes from the pod's credentials
 * (EKS Pod Identity). The policy it needs is in docs/aws-mapping.md.
 */
function iamTokenProvider(config: Config): () => Promise<string> {
  const signer = new Signer({
    hostname: config.DB_HOST,
    port: config.DB_PORT,
    username: config.DB_USER,
    region: config.AWS_REGION,
  });
  return () => signer.getAuthToken();
}

export async function pingDatabase(pool: Pool): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("SELECT 1");
  } finally {
    client.release();
  }
}
