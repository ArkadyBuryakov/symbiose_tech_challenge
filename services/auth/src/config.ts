/**
 * Typed, fail-fast configuration.
 *
 * Mirrors the Python services: everything comes from the environment and is
 * validated at startup, so a misconfigured container dies immediately with a
 * readable message. Secrets are read from *files* (mounted from `dev-keys/`
 * locally, from Secrets Manager on AWS), never from environment variables.
 */
import { readFileSync } from "node:fs";
import { z } from "zod";

/** Accepts the shell-ish booleans people actually write in .env files. */
const boolish = (fallback: "true" | "false") =>
  z
    .enum(["true", "false", "1", "0"])
    .default(fallback)
    .transform((v) => v === "true" || v === "1");

const schema = z.object({
  ENVIRONMENT: z.enum(["local", "dev", "staging", "prod"]).default("local"),
  // Accepts the uppercase levels the Python services use, so one LOG_LEVEL
  // value in .env configures the whole platform.
  LOG_LEVEL: z
    .string()
    .default("info")
    .transform((v) => v.toLowerCase())
    .pipe(z.enum(["trace", "debug", "info", "warn", "error"])),
  GIT_SHA: z.string().default("unknown"),

  HTTP_HOST: z.string().default("0.0.0.0"),
  HTTP_PORT: z.coerce.number().int().positive().default(3000),

  /** Origin the browser sees. All auth cookies are scoped to it. */
  PUBLIC_BASE_URL: z.string().url().default("http://localhost:8080"),

  DB_HOST: z.string().default("postgres"),
  DB_PORT: z.coerce.number().int().positive().default(5432),
  DB_NAME: z.string().default("pmtiles"),
  DB_USER: z.string().default("auth_svc"),
  DB_PASSWORD: z.string().min(1),
  DB_SSLMODE: z.string().default("disable"),
  /** BetterAuth owns every table in this schema. */
  DB_SCHEMA: z.string().default("auth"),
  DB_POOL_MAX: z.coerce.number().int().positive().default(10),

  BETTER_AUTH_SECRET_PATH: z.string().default("/run/keys/better-auth.secret"),
  SESSION_MAX_AGE_SECONDS: z.coerce
    .number()
    .int()
    .positive()
    .default(60 * 60 * 24 * 7),
  /** How often a still-valid session is refreshed while it is being used. */
  SESSION_UPDATE_AGE_SECONDS: z.coerce
    .number()
    .int()
    .positive()
    .default(60 * 60 * 24),

  TRUST_PROXY: boolish("true"),
});

export type Config = z.infer<typeof schema> & {
  betterAuthSecret: string;
  baseUrl: string;
};

function readSecret(path: string, what: string): string {
  let value: string;
  try {
    value = readFileSync(path, "utf8").trim();
  } catch (error) {
    throw new Error(`cannot read ${what} from ${path}: ${(error as Error).message}`);
  }
  if (!value) throw new Error(`${what} at ${path} is empty`);
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = schema.safeParse(env);
  if (!parsed.success) {
    const issues = parsed.error.issues
      .map((i) => `  ${i.path.join(".") || "(root)"}: ${i.message}`)
      .join("\n");
    throw new Error(`invalid auth service configuration:\n${issues}`);
  }
  const config = parsed.data;
  return {
    ...config,
    betterAuthSecret: readSecret(config.BETTER_AUTH_SECRET_PATH, "BetterAuth secret"),
    // BetterAuth builds callback URLs and cookie domains from this. It must be
    // the *public* origin (the edge), not the container's own address.
    baseUrl: config.PUBLIC_BASE_URL.replace(/\/+$/, ""),
  };
}
