/**
 * BetterAuth instance: users, sessions, organizations (tenants), platform
 * roles and API keys.
 *
 * The gateway is the only caller of `/internal/verify`; browsers reach the
 * `/api/auth/*` handler through the gateway, which passes those requests
 * through untouched so every `Set-Cookie` survives.
 */
import { apiKey } from "@better-auth/api-key";
import { betterAuth } from "better-auth";
import { admin, organization } from "better-auth/plugins";
import type { Pool } from "pg";
import type { Config } from "./config.js";

/** Tenant roles, in the order the organization plugin defines them. */
export const TENANT_ROLES = ["owner", "admin", "member"] as const;
export type TenantRole = (typeof TENANT_ROLES)[number];

/** Prefix on every issued API key, so a leaked key is recognisable. */
export const API_KEY_PREFIX = "pmp_";

export function createAuth(config: Config, pool: Pool) {
  return betterAuth({
    appName: "PMTiles platform",
    // The *public* origin, because that is what the browser sees and what
    // the cookies must be scoped to — not this container's address.
    baseURL: config.baseUrl,
    basePath: "/api/auth",
    secret: config.betterAuthSecret,
    database: pool,
    trustedOrigins: [config.baseUrl],

    emailAndPassword: {
      enabled: true,
      // No mail server in this environment; verification would only make
      // the demo unusable. A real deployment turns this on.
      requireEmailVerification: false,
      minPasswordLength: 10,
    },

    session: {
      expiresIn: config.SESSION_MAX_AGE_SECONDS,
      updateAge: config.SESSION_UPDATE_AGE_SECONDS,
      // Deliberately off. A signed cookie cache would let a *revoked*
      // session keep working until the cache expired, which would blow
      // past the revocation bound the platform advertises (the gateway's
      // verify-cache TTL, ~10s). Every verify hits the session table.
      cookieCache: { enabled: false },
    },

    advanced: {
      // The gateway is the only thing that talks to this service, and it sets
      // X-Forwarded-For. Without this, BetterAuth's own rate limiting cannot
      // tell callers apart and falls back to one shared bucket per path.
      ipAddress: {
        ipAddressHeaders: ["x-forwarded-for", "x-real-ip"],
      },
      // Plain HTTP locally; on AWS everything is behind TLS and this
      // becomes true via the environment.
      useSecureCookies: config.baseUrl.startsWith("https://"),
      defaultCookieAttributes: {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
      },
    },

    plugins: [
      // Organizations are the platform's tenants. Roles: owner | admin | member.
      organization({
        allowUserToCreateOrganization: false,
        organizationLimit: 10,
        membershipLimit: 200,
        creatorRole: "owner",
      }),
      // Platform administrators: `user.role === "admin"`.
      admin({
        defaultRole: "user",
        adminRoles: ["admin"],
      }),
      // Service producers. A key is owned by a user and bound to one
      // organization through its metadata; `/internal/verify` reads both.
      apiKey({
        defaultPrefix: API_KEY_PREFIX,
        enableMetadata: true,
        requireName: true,
        rateLimit: { enabled: false },
      }),
    ],
  });
}

export type Auth = ReturnType<typeof createAuth>;
