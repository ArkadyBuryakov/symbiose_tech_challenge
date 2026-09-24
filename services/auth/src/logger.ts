/**
 * Structured JSON logging, matching the field names the Python services use
 * (`service`, `level`, `event`, `request_id`) so one log query covers the
 * whole platform.
 */
import pino from "pino";

export const logger = pino({
  level: (process.env.LOG_LEVEL ?? "info").toLowerCase(),
  base: { service: "auth", version: process.env.GIT_SHA ?? "unknown" },
  messageKey: "event",
  timestamp: pino.stdTimeFunctions.isoTime,
  formatters: {
    level: (label) => ({ level: label }),
  },
  // Never let a credential reach the log.
  redact: {
    paths: [
      "req.headers.cookie",
      "req.headers.authorization",
      'req.headers["x-api-key"]',
      "res.headers['set-cookie']",
      "password",
      "key",
      "token",
    ],
    censor: "[redacted]",
  },
});

export type Logger = typeof logger;
